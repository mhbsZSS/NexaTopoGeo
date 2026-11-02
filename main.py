from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import os
import requests 
from database import get_db_connection, get_db
from models import Documento, Projeto, Cliente, Usuario
from dxf_service import gerar_croqui_dxf
from sqlalchemy.orm import Session
import uuid 
from datetime import timedelta 
from minio import Minio
from minio.error import S3Error
import stripe


# ==========================================
# CONFIGURAÇÃO DO MINIO (OBJECT STORAGE)
# ==========================================
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "admin_tomflix")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "SenhaForte123!")
MINIO_BUCKET_NAME = os.getenv("MINIO_BUCKET_NAME", "documentos-nexatopo")

# Inicializa o cliente do MinIO
minio_client = Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False # False porque estamos rodando local/Docker sem certificado SSL
)

# Garante que o bucket existe ao iniciar a aplicação
try:
    if not minio_client.bucket_exists(MINIO_BUCKET_NAME):
        minio_client.make_bucket(MINIO_BUCKET_NAME)
        print(f"Bucket '{MINIO_BUCKET_NAME}' criado com sucesso no MinIO.")
except S3Error as e:
    print(f"Erro ao conectar com o MinIO: {e}")

app = FastAPI(title="NexaTopoGeo App")

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY"))
templates = Jinja2Templates(directory="templates")

# ==========================================
# CONFIGURAÇÃO DO STRIPE (PAGAMENTOS)
# ==========================================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")
# Esta chave secreta do webhook nós vamos pegar no Stripe mais tarde!
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Endereço interno do microsserviço (definido no docker-compose.yml)
AUTH_URL = "http://auth-service:3000"
LOG_URL = os.getenv("LOG_URL", "http://log-service:4000")

# --- FUNÇÃO DE AUDITORIA ---
def disparar_log_auditoria(request: Request, usuario_id: int, acao: str):
    try:
        ip = request.client.host if request.client else "Desconhecido"
        payload = {"usuario_id": str(usuario_id), "acao": acao, "ip_origem": ip}
        requests.post(f"{LOG_URL}/log", json=payload, timeout=2)
    except Exception as e:
        print(f"Aviso: Falha ao enviar log para auditoria - {e}")

@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request):
    if request.session.get("usuario_id"):
        return RedirectResponse(url="/catalogo", status_code=303)
    return templates.TemplateResponse(request=request, name="landing.html")

@app.get("/login", response_class=HTMLResponse)
def tela_login(request: Request):
    if request.session.get("usuario_id"):
        return RedirectResponse(url="/catalogo", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")

@app.get("/cadastrar", response_class=HTMLResponse)
def tela_cadastro(request: Request):
    if request.session.get("usuario_id"):
        return RedirectResponse(url="/catalogo", status_code=303)
    return templates.TemplateResponse(request=request, name="cadastro.html")

@app.post("/cadastrar")
def cadastrar_usuario(request: Request, nome: str = Form(...), email: str = Form(...), senha: str = Form(...)):
    try:
        resposta = requests.post(f"{AUTH_URL}/cadastrar", data={"nome": nome, "email": email, "senha": senha}, timeout=10)
        if resposta.status_code == 200:
            return RedirectResponse(url="/login?msg=cadastro_sucesso", status_code=303)
        # Redireciona de volta para cadastro em caso de erro
        return RedirectResponse(url="/cadastrar?msg=erro_cadastro", status_code=303)
    except requests.exceptions.RequestException:
        return RedirectResponse(url="/cadastrar?msg=erro_offline", status_code=303)

@app.post("/login")
def realizar_login(request: Request, email: str = Form(...), senha: str = Form(...)):
    try:
        resposta = requests.post(f"{AUTH_URL}/login", data={"email": email, "senha": senha}, timeout=10)
        if resposta.status_code == 200:
            dados = resposta.json()
            request.session["usuario_id"] = dados["usuario_id"]
            request.session["role"] = dados["role"] 
            return RedirectResponse(url="/catalogo", status_code=303)
        # Redireciona em caso de credencial inválida para a tela de login
        return RedirectResponse(url="/login?msg=erro_login", status_code=303)
    except requests.exceptions.RequestException:
        return RedirectResponse(url="/login?msg=erro_offline", status_code=303)

@app.post("/esqueci-senha")
def esqueci_senha(request: Request, email: str = Form(...)):
    try:
        requests.post(f"{AUTH_URL}/esqueci-senha", data={"email": email}, timeout=10)
        return RedirectResponse(url="/login?msg=email_enviado", status_code=303)
    except requests.exceptions.RequestException:
        return RedirectResponse(url="/login?msg=erro_offline", status_code=303)
    
@app.get("/nova-senha", response_class=HTMLResponse)
def tela_nova_senha(request: Request, token: str):
    return templates.TemplateResponse(request=request, name="nova_senha.html", context={"token": token})

@app.post("/resetar-senha")
def resetar_senha(request: Request, token: str = Form(...), nova_senha: str = Form(...)):
    try:
        resposta = requests.post(f"{AUTH_URL}/resetar-senha", data={"token": token, "nova_senha": nova_senha}, timeout=10)
        if resposta.status_code == 200:
            return RedirectResponse(url="/login?msg=senha_alterada", status_code=303)
        # Captura o token inválido/expirado e devolve para o início
        return RedirectResponse(url="/login?msg=token_invalido", status_code=303)
    except requests.exceptions.RequestException:
        return RedirectResponse(url="/login?msg=erro_offline", status_code=303)
            
@app.get("/logout")
def sair(request: Request):
    usuario_id = request.session.get("usuario_id")
    
    # --- NOVO: Dispara log de auditoria de Logout antes de limpar a sessão ---
    if usuario_id:
        disparar_log_auditoria(request, usuario_id, "logout")
        
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/dashboard", response_class=HTMLResponse)
def exibir_dashboard(request: Request, db: Session = Depends(get_db)):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)

    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    nome_usuario = usuario.nome if usuario else (request.session.get("nome_usuario") or "Usuário")
    foto_url = f"/avatar/{usuario.avatar_url}" if (usuario and usuario.avatar_url) else None

    # Consulta dos projetos com dados do cliente associado
    projetos_query = (
        db.query(Projeto, Cliente.nome_razao.label("cliente_nome"))
        .join(Cliente, Projeto.cliente_id == Cliente.id)
        .all()
    )

    # Agrupa projetos nas 5 etapas do Kanban
    colunas_kanban = ["orcamento", "campo", "desenho", "cartorio", "concluido"]
    projetos_por_status = {coluna: [] for coluna in colunas_kanban}

    for projeto, cliente_nome in projetos_query:
        status_key = projeto.status.lower() if projeto.status else "orcamento"
        if status_key not in projetos_por_status:
            projetos_por_status[status_key] = []
        projetos_por_status[status_key].append({
            "id": projeto.id,
            "titulo": projeto.titulo,
            "cliente_nome": cliente_nome,
            "area_hectares": projeto.area_hectares,
            "status": status_key,
            "criado_em": projeto.criado_em.strftime("%d/%m/%Y") if projeto.criado_em else ""
        })

    return templates.TemplateResponse(request, "dashboard.html", {
        "projetos_por_status": projetos_por_status,
        "nome_usuario": nome_usuario,
        "foto_url": foto_url,
        "role": request.session.get("role", "usuario"),
        "is_premium": usuario.is_premium if usuario else False
    })


# ==========================================
# ROTA DE CRIAÇÃO DE PROJETOS (ISSUE #5)
# ==========================================
@app.post("/projetos/novo")
def criar_novo_projeto(
    request: Request,
    titulo: str = Form(...),
    cliente_nome: str = Form(...),
    db: Session = Depends(get_db)
):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)

    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not usuario:
        return RedirectResponse(url="/login", status_code=303)

    # 1. Trava Freemium (Business Logic)
    qtd_projetos = db.query(Projeto).filter(Projeto.usuario_id_tecnico == usuario_id).count()

    # Se não for assinante Pro e já atingiu o limite de 2 projetos
    if not usuario.is_premium and qtd_projetos >= 2:
        return RedirectResponse(url="/perfil?msg=limite_atingido", status_code=303)

    # 2. Criação do Cliente e Projeto
    cliente = (
        db.query(Cliente)
        .filter(Cliente.usuario_id == usuario_id, Cliente.nome_razao == cliente_nome)
        .first()
    )

    if not cliente:
        cliente = Cliente(
            usuario_id=usuario_id,
            nome_razao=cliente_nome,
            cpf_cnpj=f"TEMP-{uuid.uuid4().hex[:12]}",
            telefone=""
        )
        db.add(cliente)
        db.commit()
        db.refresh(cliente)

    novo_projeto = Projeto(
        cliente_id=cliente.id,
        usuario_id_tecnico=usuario_id,
        titulo=titulo,
        status="orcamento"
    )
    db.add(novo_projeto)
    db.commit()
    db.refresh(novo_projeto)

    disparar_log_auditoria(request, usuario_id, f"criou_projeto_{novo_projeto.id}")

    return RedirectResponse(url="/dashboard?msg=projeto_criado", status_code=303)

@app.get("/auditoria", response_class=HTMLResponse)
def painel_auditoria(request: Request):
    usuario_id = request.session.get("usuario_id")
    role = request.session.get("role")

    # 1. Verifica se está logado
    if not usuario_id:
        return RedirectResponse(url="/", status_code=303)
        
    # ==========================================
    # 2. ENFORCEMENT (Obrigatório para o Requisito 5)
    # ==========================================
    if role != "admin":
        # Dispara um log rastreando a tentativa de invasão ao painel
        disparar_log_auditoria(request, usuario_id, "tentativa_acesso_painel_auditoria_403")
        raise HTTPException(
            status_code=403, 
            detail="Acesso negado: Rota exclusiva para administradores do sistema."
        )

    # 3. Consome os dados do microsserviço de log
    try:
        # Faz um GET na porta 4000 do contêiner log-service
        resposta = requests.get(f"{LOG_URL}/logs?limite=100", timeout=5)
        logs = resposta.json().get("logs", []) if resposta.status_code == 200 else []
    except Exception as e:
        print(f"Erro ao buscar logs: {e}")
        logs = []

    # 4. Busca os nomes dos usuários para a tabela ficar legível
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, nome FROM usuarios")
    usuarios_db = {str(u['id']): u['nome'] for u in cursor.fetchall()}
    cursor.close()
    conn.close()

    # Enriquece os logs com os nomes reais
    for log in logs:
        uid = log.get("usuario_id")
        log["nome_usuario"] = usuarios_db.get(uid, f"ID {uid}")

    return templates.TemplateResponse(
        request, 
        "auditoria.html", 
        {
            "logs": logs,
            "role": role,
            "nome_usuario": request.session.get("nome_usuario", "Admin")
        }
    )

@app.get("/avatar/{nome_arquivo}")
def obter_avatar(nome_arquivo: str):
    try:
        # O backend busca a imagem diretamente na rede fechada do Docker
        resposta = minio_client.get_object(MINIO_BUCKET_NAME, nome_arquivo)
        return StreamingResponse(resposta.stream(32*1024), media_type="image/jpeg")
    except Exception:
        raise HTTPException(status_code=404, detail="Avatar não encontrado")
    
@app.get("/perfil", response_class=HTMLResponse)
def pagina_perfil(request: Request, db: Session = Depends(get_db)):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)

    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not usuario:
        return RedirectResponse(url="/login", status_code=303)

    foto_url = None
    if usuario.avatar_url:
        foto_url = f"/avatar/{usuario.avatar_url}"

    return templates.TemplateResponse(request, "perfil.html", {
        "usuario": usuario, 
        "foto_url": foto_url,
        "nome_usuario": usuario.nome,
        "role": request.session.get("role", usuario.role)
    })

@app.post("/perfil/editar")
async def editar_perfil(
    request: Request,
    bio: str = Form(""),
    foto: UploadFile = File(None)
):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        raise HTTPException(status_code=401, detail="Não autorizado")

    nome_arquivo = None

    if foto and foto.filename:
        if not foto.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Apenas ficheiros de imagem (JPG, PNG, etc.) são permitidos.")
        
        foto.file.seek(0, 2) 
        tamanho = foto.file.tell() 
        foto.file.seek(0) 
        
        if tamanho > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="A imagem deve ter no máximo 2MB.")

        extensao = foto.filename.split(".")[-1]
        nome_arquivo = f"perfil_{usuario_id}_{uuid.uuid4().hex}.{extensao}"

        try:
            minio_client.put_object(
                MINIO_BUCKET_NAME,
                nome_arquivo,
                foto.file,
                length=tamanho,
                content_type=foto.content_type
            )
        except S3Error as e:
            print(f"Erro no upload do MinIO: {e}")
            raise HTTPException(status_code=500, detail="Erro ao salvar a imagem no servidor.")

    conn = get_db_connection()
    cursor = conn.cursor()
    
    if nome_arquivo:
        # CORREÇÃO 3: Atualiza a coluna correta (avatar_url)
        cursor.execute(
            "UPDATE usuarios SET bio = %s, avatar_url = %s WHERE id = %s", 
            (bio, nome_arquivo, usuario_id)
        )
    else:
        cursor.execute(
            "UPDATE usuarios SET bio = %s WHERE id = %s", 
            (bio, usuario_id)
        )
        
    conn.commit()
    cursor.close()
    conn.close()

    return RedirectResponse(url="/perfil", status_code=303)


# ==========================================
# ROTAS DE UPLOAD DE DOCUMENTOS (ISSUE #2)
# ==========================================
MAPA_EXTENSOES_DOCUMENTOS = {
    ".csv": "sigef_csv",
    ".xlsx": "sigef_xlsx",
    ".dwg": "croqui_dwg",
    ".dxf": "croqui_dxf",
    ".pdf": "memorial_pdf",
}

async def processar_upload_documento(
    projeto_id: int,
    request: Request,
    file: UploadFile,
    db: Session
):
    # 4. SEGURANÇA: Autenticação e RBAC (técnico ou admin)
    usuario_id = request.session.get("usuario_id")
    role = request.session.get("role")

    # Suporte a JWT via header Authorization (Bearer Token)
    if not usuario_id and "authorization" in request.headers:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            try:
                from jose import jwt
                payload = jwt.decode(token, os.getenv("SECRET_KEY", ""), algorithms=["HS256"])
                usuario_id = payload.get("usuario_id") or payload.get("sub")
                role = payload.get("role")
            except Exception:
                pass

    if not usuario_id:
        raise HTTPException(status_code=401, detail="Não autorizado")

    if role not in ["admin", "tecnico", "usuario"]:
        raise HTTPException(
            status_code=403, 
            detail="Acesso negado: apenas técnicos ou administradores podem fazer upload de documentos."
        )

    # 2. VALIDAÇÃO DE EXTENSÕES
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Arquivo não fornecido.")

    _, ext = os.path.splitext(file.filename)
    ext = ext.lower()

    if ext not in MAPA_EXTENSOES_DOCUMENTOS:
        raise HTTPException(
            status_code=400,
            detail=f"Extensão '{ext}' não permitida. Apenas {', '.join(MAPA_EXTENSOES_DOCUMENTOS.keys())} são aceitas."
        )

    tipo = MAPA_EXTENSOES_DOCUMENTOS[ext]

    file.file.seek(0, 2)
    tamanho = file.file.tell()
    file.file.seek(0)

    minio_object_name = f"projeto_{projeto_id}_{uuid.uuid4().hex}{ext}"

    # 1. UPLOAD NO MINIO
    try:
        minio_client.put_object(
            MINIO_BUCKET_NAME,
            minio_object_name,
            file.file,
            length=tamanho,
            content_type=file.content_type or "application/octet-stream"
        )
    except S3Error as e:
        print(f"Erro no upload do MinIO: {e}")
        raise HTTPException(status_code=500, detail="Erro ao salvar o documento no servidor de storage.")

    # 3. REGISTRO NO BANCO DE DADOS
    documento = Documento(
        projeto_id=projeto_id,
        nome_arquivo=file.filename,
        minio_object_name=minio_object_name,
        tipo=tipo
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    disparar_log_auditoria(request, usuario_id, f"upload_documento_{documento.id}_projeto_{projeto_id}")

    return {
        "status": "sucesso",
        "mensagem": "Upload realizado com sucesso",
        "documento": {
            "id": documento.id,
            "projeto_id": documento.projeto_id,
            "nome_arquivo": documento.nome_arquivo,
            "minio_object_name": documento.minio_object_name,
            "tipo": documento.tipo,
            "upload_em": documento.upload_em.isoformat() if documento.upload_em else None
        }
    }


@app.post("/projetos/{projeto_id}/upload")
async def upload_documento_projeto(
    projeto_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Upload de documento via path parameter (ex: /projetos/1/upload)."""
    return await processar_upload_documento(projeto_id, request, file, db)


@app.post("/documentos/upload")
async def upload_documento_form(
    request: Request,
    projeto_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Upload de documento via multipart/form-data."""
    return await processar_upload_documento(projeto_id, request, file, db)


@app.post("/projetos/{projeto_id}/gerar-croqui")
async def gerar_croqui_projeto(
    projeto_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    # 4. SEGURANÇA: Autenticação e RBAC (admin/técnico)
    usuario_id = request.session.get("usuario_id")
    role = request.session.get("role")

    if not usuario_id and "authorization" in request.headers:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            try:
                from jose import jwt
                payload = jwt.decode(token, os.getenv("SECRET_KEY", ""), algorithms=["HS256"])
                usuario_id = payload.get("usuario_id") or payload.get("sub")
                role = payload.get("role")
            except Exception:
                pass

    if not usuario_id:
        raise HTTPException(status_code=401, detail="Não autorizado")

    if role not in ["admin", "tecnico", "usuario"]:
        raise HTTPException(
            status_code=403,
            detail="Acesso negado: apenas técnicos ou administradores podem gerar croquis."
        )

    # Coordenadas fictícias simulando leitura de dados do SIGEF
    pontos_mock = [
        {"vertice": "P01", "x": 100.0, "y": 100.0},
        {"vertice": "P02", "x": 250.0, "y": 100.0},
        {"vertice": "P03", "x": 250.0, "y": 250.0},
        {"vertice": "P04", "x": 100.0, "y": 200.0},
    ]

    nome_arquivo_dxf = f"croqui_projeto_{projeto_id}.dxf"
    minio_object_name = f"projeto_{projeto_id}_croqui_{uuid.uuid4().hex}.dxf"

    caminho_local_dxf = None
    try:
        # Gera o arquivo DXF local temporário
        caminho_local_dxf = gerar_croqui_dxf(nome_arquivo_dxf, pontos_mock)

        # Upload do arquivo DXF gerado para o MinIO
        minio_client.fput_object(
            MINIO_BUCKET_NAME,
            minio_object_name,
            caminho_local_dxf,
            content_type="application/dxf"
        )
    except S3Error as e:
        print(f"Erro no upload do MinIO: {e}")
        raise HTTPException(status_code=500, detail="Erro ao salvar o croqui no servidor de storage.")
    except Exception as e:
        print(f"Erro ao processar croqui DXF: {e}")
        raise HTTPException(status_code=500, detail=f"Erro na geração do croqui DXF: {str(e)}")
    finally:
        # Apaga o arquivo DXF temporário local após o upload para o MinIO
        if caminho_local_dxf and os.path.exists(caminho_local_dxf):
            try:
                os.remove(caminho_local_dxf)
            except OSError as err:
                print(f"Aviso ao remover temporário: {err}")

    # Registra o novo arquivo no banco de dados na tabela Documento com o tipo 'croqui_dxf'
    documento = Documento(
        projeto_id=projeto_id,
        nome_arquivo=nome_arquivo_dxf,
        minio_object_name=minio_object_name,
        tipo="croqui_dxf"
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    disparar_log_auditoria(request, usuario_id, f"gerou_croqui_dxf_projeto_{projeto_id}")

    return {
        "status": "sucesso",
        "mensagem": "Croqui DXF gerado e armazenado com sucesso",
        "documento": {
            "id": documento.id,
            "projeto_id": documento.projeto_id,
            "nome_arquivo": documento.nome_arquivo,
            "minio_object_name": documento.minio_object_name,
            "tipo": documento.tipo,
            "upload_em": documento.upload_em.isoformat() if documento.upload_em else None
        }
    }


# ==========================================
# ROTAS DE ASSINATURA (STRIPE) - ATIVIDADE 7
# ==========================================

@app.post("/checkout")
def criar_checkout_session(request: Request):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/", status_code=303)
        
    try:
        # Captura o domínio atual automaticamente (ex: http://localhost:8000)
        domain_url = str(request.base_url).rstrip("/")
        
        # Cria a sessão no Stripe
        checkout_session = stripe.checkout.Session.create(
            line_items=[
                {
                    'price': STRIPE_PRICE_ID,
                    'quantity': 1,
                },
            ],
            mode='subscription',
            # Redireciona de volta após o sucesso ou cancelamento
            success_url=domain_url + '/dashboard?msg=assinatura_sucesso',
            cancel_url=domain_url + '/perfil?msg=assinatura_cancelada',
            # O Pulo do Gato: mandamos o ID do usuário oculto para o Stripe
            client_reference_id=str(usuario_id) 
        )
        
        # Redireciona o usuário para a página de pagamento segura do Stripe
        return RedirectResponse(url=checkout_session.url, status_code=303)
        
    except Exception as e:
        print(f"Erro ao criar sessão do Stripe: {e}")
        return RedirectResponse(url="/perfil?msg=erro_stripe", status_code=303)


@app.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Esta rota não é acessada pelo usuário, é o Stripe que a chama 
    por debaixo dos panos quando o pagamento é aprovado.
    """
    # O Stripe exige o body cru (raw) para validar a assinatura de segurança
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        # Validação obrigatória de segurança
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        # Payload inválido
        raise HTTPException(status_code=400, detail="Payload inválido")
    except stripe.error.SignatureVerificationError as e:
        # Assinatura de segurança falsa/inválida
        raise HTTPException(status_code=400, detail="Assinatura inválida")

    # Verifica se a notificação é de um pagamento concluído com sucesso
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        # Lembra que mandamos o ID do usuário oculto? Agora o Stripe nos devolve!
        usuario_id = session.get("client_reference_id")
        customer_id = session.get("customer")

        if usuario_id:
            # Efetiva o benefício atualizando o banco de dados
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "UPDATE usuarios SET is_premium = TRUE, stripe_customer_id = %s WHERE id = %s",
                    (customer_id, usuario_id)
                )
                conn.commit()
                print(f"Sucesso! Usuário {usuario_id} agora é PREMIUM.")
            except Exception as e:
                print(f"Erro ao atualizar banco no webhook: {e}")
            finally:
                cursor.close()
                conn.close()

    # O Stripe espera um status 200 para saber que recebemos a mensagem
    return {"status": "sucesso"}
