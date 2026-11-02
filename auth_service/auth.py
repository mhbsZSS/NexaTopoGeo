import requests
from fastapi import FastAPI, HTTPException, Request
import mysql.connector
import bcrypt
import uuid
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

app = FastAPI(title="Microsserviço de Autenticação")

LOG_URL = os.getenv("LOG_URL", "http://log-service:4000")

# --- FUNÇÃO DE AUDITORIA ---
def disparar_log_auditoria(request: Request, usuario_id: int, acao: str):
    try:
        ip = request.client.host if request and request.client else "Desconhecido"
        payload = {"usuario_id": str(usuario_id), "acao": acao, "ip_origem": ip}
        requests.post(f"{LOG_URL}/log", json=payload, timeout=2)
    except Exception as e:
        print(f"Aviso: Falha ao enviar log para auditoria - {e}")

def get_db_connection():
    """Cria a conexão isolada do microsserviço com o banco de dados."""
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

async def obter_payload(request: Request) -> dict:
    """Extrai os parâmetros da requisição tanto no formato JSON quanto Form-Data."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return await request.json()
        except Exception:
            return {}
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        return {}

@app.post("/cadastrar")
async def cadastrar(request: Request):
    """Recebe os dados cadastrais, criptografa a senha e salva no banco."""
    dados = await obter_payload(request)
    nome = dados.get("nome")
    email = dados.get("email")
    senha = dados.get("senha")

    if not nome or not email or not senha:
        raise HTTPException(status_code=400, detail="Campos nome, email e senha são obrigatórios.")

    conn = get_db_connection()
    cursor = conn.cursor()
    
    salt = bcrypt.gensalt()
    senha_hash = bcrypt.hashpw(senha.encode('utf-8'), salt).decode('utf-8')
    
    try:
        cursor.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, role) VALUES (%s, %s, %s, %s)", 
            (nome, email, senha_hash, 'usuario')
        )
        conn.commit()
    except Exception as e:
        cursor.close()
        conn.close()
        print(f"[DEBUG AUTH-SERVICE] Erro ao inserir usuário ({email}): {e}")
        raise HTTPException(status_code=400, detail="E-mail já cadastrado")
    
    cursor.close()
    conn.close()
    return {"status": "sucesso"}

@app.post("/login")
async def login(request: Request):
    """Valida as credenciais, dispara o log e devolve o ID e o Papel (Role) do usuário."""
    dados = await obter_payload(request)
    email = dados.get("email")
    senha = dados.get("senha")

    if not email or not senha:
        raise HTTPException(status_code=400, detail="E-mail e senha são obrigatórios.")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT id, senha_hash, role FROM usuarios WHERE email = %s", (email,))
    user = cursor.fetchone()
    
    cursor.close()
    conn.close()

    # Verifica se o usuário existe e se a senha bate
    if user and bcrypt.checkpw(senha.encode('utf-8'), user['senha_hash'].encode('utf-8')):
        usuario_id = user['id']
        role = user['role']

        # Dispara o log ANTES do return
        disparar_log_auditoria(request, usuario_id, "login")
            
        return {"status": "sucesso", "usuario_id": usuario_id, "role": role}
    
    # Se falhar, devolve o erro 401
    raise HTTPException(status_code=401, detail="E-mail ou senha inválidos")

@app.post("/esqueci-senha")
async def esqueci_senha(request: Request):
    """Gera um token UUID único, salva no banco e dispara o e-mail HTML via Brevo."""
    dados = await obter_payload(request)
    email = dados.get("email")

    if not email:
        raise HTTPException(status_code=400, detail="E-mail é obrigatório.")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
    user = cursor.fetchone()
    
    if not user:
        cursor.close()
        conn.close()
        return {"status": "sucesso", "message": "Se o e-mail existir, um link foi enviado."}

    token = str(uuid.uuid4())
    expira_em = datetime.now() + timedelta(minutes=30)
    
    cursor.execute(
        "INSERT INTO reset_tokens (token, usuario_id, expira_em) VALUES (%s, %s, %s)",
        (token, user['id'], expira_em)
    )
    conn.commit()
    cursor.close()
    conn.close()

    # Credenciais do Brevo vindas do ambiente
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)
    
    app_url = os.getenv("APP_URL", "https://marcio-silva-isw055.lapps.studio")
    link = f"{app_url}/nova-senha?token={token}"
    
    # Construção do E-mail com suporte a HTML
    msg = MIMEMultipart("alternative")
    msg['Subject'] = 'Redefinição de Senha - NexaTopoGeo'
    msg['From'] = f"NexaTopoGeo Suporte <{smtp_from}>"
    msg['To'] = email

    # Versão em texto simples (fallback de segurança)
    texto_simples = f"Acesse o link para redefinir sua senha: {link} (Expira em 30 minutos)."

    # Versão em HTML estilizada
    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #F8F9FA; color: #333333; padding: 30px; text-align: center;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; border-top: 4px solid #2E472D; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
          <h1 style="color: #2E472D; margin-top: 0;">NexaTopoGeo</h1>
          <h3 style="color: #333333;">Recuperação de Senha</h3>
          <p style="color: #666666; font-size: 15px; line-height: 1.5;">
            Recebemos uma solicitação para redefinir a senha da sua conta. Clique no botão abaixo para criar uma nova senha:
          </p>
          <div style="margin: 30px 0;">
            <a href="{link}" style="background-color: #2E472D; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px; display: inline-block;">
              Redefinir Minha Senha
            </a>
          </div>
          <p style="color: #888888; font-size: 12px;">
            Este link é válido por <strong>30 minutos</strong>. Se você não solicitou esta alteração, ignore este e-mail com segurança.
          </p>
        </div>
      </body>
    </html>
    """

    msg.attach(MIMEText(texto_simples, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp-relay.brevo.com", 587, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [email], msg.as_string())
    except Exception as e:
        print(f"Erro SMTP Brevo: {e}")
        raise HTTPException(status_code=500, detail="Erro ao conectar com o serviço de e-mail.")
    
    return {"status": "sucesso", "message": "E-mail de recuperação enviado."}

@app.post("/resetar-senha")
async def resetar_senha(request: Request):
    """Valida o token temporal e aplica o Hash na nova senha."""
    dados = await obter_payload(request)
    token = dados.get("token")
    nova_senha = dados.get("nova_senha")

    if not token or not nova_senha:
        raise HTTPException(status_code=400, detail="Token e nova senha são obrigatórios.")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Verifica 3 coisas simultaneamente: token existe? não foi usado?
    cursor.execute("SELECT * FROM reset_tokens WHERE token = %s AND usado = FALSE", (token,))
    registro = cursor.fetchone()
    
    if not registro:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Token inválido ou já utilizado.")
        
    # Verifica a expiração temporal
    if datetime.now() > registro['expira_em']:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Este link de recuperação expirou.")

    # Criptografa a nova senha
    salt = bcrypt.gensalt()
    senha_hash = bcrypt.hashpw(nova_senha.encode('utf-8'), salt).decode('utf-8')
    
    # Atualiza a senha na tabela principal e queima o token na tabela secundária
    cursor.execute("UPDATE usuarios SET senha_hash = %s WHERE id = %s", (senha_hash, registro['usuario_id']))
    cursor.execute("UPDATE reset_tokens SET usado = TRUE WHERE token = %s", (token,))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return {"status": "sucesso", "message": "Senha atualizada com êxito."}
