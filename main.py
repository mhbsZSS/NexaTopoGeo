from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from typing import Optional
import os
import tempfile
import requests 
import csv
import io
import re
import PyPDF2
from docxtpl import DocxTemplate
from database import get_db_connection, get_db, engine
from models import Documento, Projeto, Cliente, Usuario, Vertice
from dxf_service import gerar_croqui_dxf, _converter_coord
from sqlalchemy.orm import Session
from sqlalchemy import text
import uuid 
from datetime import timedelta 
from minio import Minio
from minio.error import S3Error
import stripe
from database import engine
import models
import bcrypt

models.Base.metadata.create_all(bind=engine)

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

try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE vertices ADD COLUMN IF NOT EXISTS origem VARCHAR(50) DEFAULT 'SIGEF'"))
        conn.commit()
except Exception as e:
    print(f"[DB AUTO-MIGRATION] Falha ou coluna já existente: {e}")

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
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="landing.html")

@app.get("/login", response_class=HTMLResponse)
def tela_login(request: Request):
    if request.session.get("usuario_id"):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")

@app.get("/cadastrar", response_class=HTMLResponse)
def tela_cadastro(request: Request):
    if request.session.get("usuario_id"):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="cadastro.html")

@app.get("/catalogo")
def redirecionar_catalogo():
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/cadastrar")
def cadastrar_usuario(request: Request, nome: str = Form(...), email: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    try:
        # Verifica se o email já existe no banco
        usuario_existente = db.query(Usuario).filter(Usuario.email == email).first()
        if usuario_existente:
            return RedirectResponse(url="/cadastrar?msg=erro_cadastro", status_code=303)
        
        # Encripta a password com bcrypt
        salt = bcrypt.gensalt()
        senha_hash = bcrypt.hashpw(senha.encode('utf-8'), salt).decode('utf-8')
        
        # Cria e guarda o utilizador diretamente no banco de dados do NexaTopoGeo
        novo_usuario = Usuario(nome=nome, email=email, senha_hash=senha_hash, is_premium=False, role="usuario")
        db.add(novo_usuario)
        db.commit()
        db.refresh(novo_usuario)
        
        return RedirectResponse(url="/login?msg=cadastro_sucesso", status_code=303)
        
    except Exception as e:
        print(f"[DEBUG AUTH] Erro ao gravar usuário no BD: {e}")
        db.rollback()
        return RedirectResponse(url="/cadastrar?msg=erro_offline", status_code=303)

@app.post("/login")
def realizar_login(request: Request, email: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    try:
        # Busca o usuário no banco de dados pelo email
        usuario = db.query(Usuario).filter(Usuario.email == email).first()
        
        # Se não encontrar o usuário, ou se a senha não bater (usando bcrypt para verificar)
        if not usuario or not bcrypt.checkpw(senha.encode('utf-8'), usuario.senha_hash.encode('utf-8')):
            return RedirectResponse(url="/login?msg=erro_login", status_code=303)
        
        # Sucesso! Grava a sessão do usuário
        request.session["usuario_id"] = usuario.id
        request.session["role"] = usuario.role
        request.session["nome_usuario"] = usuario.nome
        
        # Opcional: dispara o log de auditoria
        disparar_log_auditoria(request, usuario.id, "login_sucesso")
        
        return RedirectResponse(url="/dashboard", status_code=303)
        
    except Exception as e:
        print(f"[DEBUG AUTH] Erro ao realizar login no BD: {e}")
        return RedirectResponse(url="/login?msg=erro_offline", status_code=303)

@app.post("/esqueci-senha")
def esqueci_senha(request: Request, email: str = Form(...)):
    try:
        resposta = requests.post(f"{AUTH_URL}/esqueci-senha", json={"email": email}, timeout=10)
        if resposta.status_code == 200:
            return RedirectResponse(url="/login?msg=email_enviado", status_code=303)
        
        print(f"[DEBUG AUTH] Erro no esqueci-senha: {resposta.text}")
        return RedirectResponse(url="/login?msg=email_enviado", status_code=303)
    except requests.exceptions.RequestException as e:
        print(f"[DEBUG AUTH] Erro de rede no esqueci-senha: {e}")
        return RedirectResponse(url="/login?msg=erro_offline", status_code=303)
    
@app.get("/nova-senha", response_class=HTMLResponse)
def tela_nova_senha(request: Request, token: str):
    return templates.TemplateResponse(request=request, name="nova_senha.html", context={"token": token})

@app.post("/resetar-senha")
def resetar_senha(request: Request, token: str = Form(...), nova_senha: str = Form(...)):
    try:
        resposta = requests.post(f"{AUTH_URL}/resetar-senha", json={"token": token, "nova_senha": nova_senha}, timeout=10)
        if resposta.status_code == 200:
            return RedirectResponse(url="/login?msg=senha_alterada", status_code=303)
        
        print(f"[DEBUG AUTH] Erro no resetar-senha: {resposta.text}")
        return RedirectResponse(url="/login?msg=token_invalido", status_code=303)
    except requests.exceptions.RequestException as e:
        print(f"[DEBUG AUTH] Erro de rede no resetar-senha: {e}")
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
    
    # FORÇAR MODO FREE PARA TESTE: (Remova ou comente esta linha após o teste)
    usuario.is_premium = False 

    # Se não for assinante Pro e já atingiu o limite de 3 projetos
    if not usuario.is_premium and qtd_projetos >= 3:
        # Usar RedirectResponse com msg=limite_projetos
        return RedirectResponse(url="/dashboard?msg=limite_projetos", status_code=303)

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


# ==========================================
# DETALHES E STATUS DO PROJETO (KANBAN/SIGEF)
# ==========================================
@app.get("/projetos/{projeto_id}", response_class=HTMLResponse)
def detalhe_projeto(projeto_id: int, request: Request, db: Session = Depends(get_db)):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)

    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    nome_usuario = usuario.nome if usuario else (request.session.get("nome_usuario") or "Usuário")
    foto_url = f"/avatar/{usuario.avatar_url}" if (usuario and usuario.avatar_url) else None

    # Busca o projeto no banco sem filtrar por status, garantindo acesso em qualquer fase
    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if not projeto:
        return RedirectResponse(url="/dashboard?msg=projeto_nao_encontrado", status_code=303)

    cliente = projeto.cliente if projeto.cliente else db.query(Cliente).filter(Cliente.id == projeto.cliente_id).first()
    documentos = (
        db.query(Documento)
        .filter(Documento.projeto_id == projeto_id)
        .order_by(Documento.upload_em.desc())
        .all()
    )
    vertices = (
        db.query(Vertice)
        .filter(Vertice.projeto_id == projeto_id)
        .order_by(Vertice.id.asc())
        .all()
    )

    return templates.TemplateResponse(request, "projeto_detalhe.html", {
        "projeto": projeto,
        "cliente": cliente,
        "documentos": documentos,
        "vertices": vertices,
        "usuario": usuario,
        "nome_usuario": nome_usuario,
        "foto_url": foto_url,
        "role": request.session.get("role", "usuario"),
        "is_premium": usuario.is_premium if usuario else False
    })


@app.post("/projetos/{projeto_id}/atualizar-status")
def atualizar_status_projeto(
    projeto_id: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db)
):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)

    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if not projeto:
        return RedirectResponse(url="/dashboard?msg=projeto_nao_encontrado", status_code=303)

    status_normalizado = status.strip().lower()
    status_permitidos = ["orcamento", "campo", "desenho", "cartorio", "concluido"]
    if status_normalizado in status_permitidos:
        projeto.status = status_normalizado
        db.commit()
        db.refresh(projeto)
        disparar_log_auditoria(request, usuario_id, f"atualizou_status_projeto_{projeto_id}_para_{status_normalizado}")

    return RedirectResponse(url=f"/projetos/{projeto_id}?msg=status_atualizado", status_code=303)


@app.post("/projetos/{projeto_id}/atualizar-dados")
def atualizar_dados_projeto(
    projeto_id: int,
    request: Request,
    projeto_titulo: str = Form(...),
    cliente_nome: str = Form(...),
    area_hectares: Optional[str] = Form(None),
    valor_orcamento: Optional[str] = Form(None),
    perimetro: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)

    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if not projeto:
        return RedirectResponse(url="/dashboard?msg=projeto_nao_encontrado", status_code=303)

    # Atualiza o título do projeto
    projeto.titulo = projeto_titulo.strip()

    # Procura e atualiza o Cliente correspondente
    cliente = None
    if projeto.cliente:
        cliente = projeto.cliente
    elif projeto.cliente_id:
        cliente = db.query(Cliente).filter(Cliente.id == projeto.cliente_id).first()

    if cliente:
        cliente.nome_razao = cliente_nome.strip()
    else:
        cliente = Cliente(
            usuario_id=usuario_id,
            nome_razao=cliente_nome.strip(),
            cpf_cnpj=f"TEMP-{uuid.uuid4().hex[:12]}",
            telefone=""
        )
        db.add(cliente)
        db.commit()
        db.refresh(cliente)
        projeto.cliente_id = cliente.id

    # Atualiza área em hectares se fornecida
    if area_hectares is not None and str(area_hectares).strip() != "":
        try:
            area_val = float(str(area_hectares).strip().replace(',', '.'))
            if area_val >= 0:
                projeto.area_hectares = area_val
        except ValueError:
            pass

    # Atualiza valor do orçamento se fornecido
    if valor_orcamento is not None and str(valor_orcamento).strip() != "":
        try:
            valor_val = float(str(valor_orcamento).strip().replace(',', '.'))
            if valor_val >= 0:
                projeto.valor_orcamento = valor_val
        except ValueError:
            pass

    # Atualiza perímetro se fornecido
    if perimetro is not None and str(perimetro).strip() != "":
        try:
            perimetro_val = float(str(perimetro).strip().replace(',', '.'))
            if perimetro_val >= 0:
                projeto.perimetro = perimetro_val
        except ValueError:
            pass

    db.commit()
    db.refresh(projeto)
    disparar_log_auditoria(request, usuario_id, f"atualizou_dados_projeto_{projeto_id}")

    return RedirectResponse(url=f"/projetos/{projeto_id}?msg=dados_atualizados", status_code=303)


# ==========================================
# PROCESSAMENTO DE PLANILHA / RELATÓRIO SIGEF (CSV / PDF)
# ==========================================
@app.post("/projetos/{projeto_id}/processar-sigef")
async def processar_sigef(projeto_id: int, request: Request, db: Session = Depends(get_db)):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        raise HTTPException(status_code=401, detail="Não autorizado. Faça login primeiro.")

    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if not projeto:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")

    # a) Buscar o último documento do tipo sigef_csv OU memorial_pdf vinculado ao projeto
    documento = (
        db.query(Documento)
        .filter(Documento.projeto_id == projeto_id, Documento.tipo.in_(["sigef_csv", "memorial_pdf"]))
        .order_by(Documento.upload_em.desc())
        .first()
    )

    if not documento:
        raise HTTPException(
            status_code=404,
            detail="Nenhum arquivo SIGEF (.csv ou .pdf) encontrado para este projeto. Faça o upload antes de processar."
        )

    # b) Fazer download do arquivo no MinIO
    try:
        minio_response = minio_client.get_object(MINIO_BUCKET_NAME, documento.minio_object_name)
        conteudo_bytes = minio_response.read()
        minio_response.close()
        minio_response.release_conn()
    except S3Error as e:
        print(f"[ERRO MINIO] Falha ao recuperar arquivo SIGEF: {e}")
        raise HTTPException(status_code=500, detail="Erro ao acessar o arquivo no servidor de armazenamento.")
    except Exception as e:
        print(f"[ERRO DOWNLOAD] Erro inesperado ao baixar do MinIO: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao recuperar arquivo do MinIO: {str(e)}")

    novos_vertices = []
    area_calculada = None

    # -------------------------------------------------------------
    # 1. PROCESSAMENTO DE RELATÓRIO PDF (memorial_pdf) VIA REGEX
    # -------------------------------------------------------------
    if documento.tipo == "memorial_pdf":
        import tempfile
        texto_bruto = ""
        tmp_pdf_path = None
        try:
            # 1. Salva o stream de bytes num arquivo físico temporário
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                tmp_pdf.write(conteudo_bytes)
                tmp_pdf_path = tmp_pdf.name
            
            # 2. Lê o arquivo físico
            reader = PyPDF2.PdfReader(tmp_pdf_path)
            texto_bruto = "\n".join([page.extract_text() or "" for page in reader.pages])
            
            # 3. Limpeza do arquivo temporário
            if os.path.exists(tmp_pdf_path):
                os.remove(tmp_pdf_path)
                
            if not texto_bruto.strip():
                raise ValueError("O PyPDF2 não conseguiu extrair nenhum texto legível deste PDF.")
                
        except Exception as e:
            if tmp_pdf_path and os.path.exists(tmp_pdf_path):
                try:
                    os.remove(tmp_pdf_path)
                except OSError:
                    pass
            # Lança o erro exato para o frontend mostrar no alerta
            raise HTTPException(status_code=400, detail=f"Falha na leitura do PDF: {str(e)}")

        # 1. Leitura e Limpeza Inicial: substitui quebras de linha e múltiplos espaços por um espaço simples
        texto_limpo = re.sub(r'\s+', ' ', texto_bruto).strip()

        if not texto_limpo:
            raise HTTPException(status_code=400, detail="Falha na leitura do PDF: O conteúdo extraído está vazio.")

        # Extração de Área Total (se presente no memorial)
        match_area = re.search(
            r'(?:área|area)(?:\s+total)?(?:\s+\(.*?local.*?\))?(?:\s+\(.*?\))?\s*[:=;]?\s*([\d]+(?:[.,]\d+)?)\s*(?:ha|hectares?)?',
            texto_limpo,
            re.IGNORECASE
        )
        if match_area:
            try:
                area_calculada = float(match_area.group(1).replace(',', '.'))
                print(f"[DEBUG PDF] Área calculada identificada: {area_calculada} ha")
            except ValueError:
                pass

        # Isole a seção "DESCRIÇÃO DA PARCELA" se identificada no texto limpo
        match_secao = re.search(r'DESCRI[CÇ][AÃ]O\s+D[AEO]\s+(?:PARCELA|PER[IÍ]METRO|LIMITES)(.*)', texto_limpo, re.IGNORECASE)
        texto_busca = match_secao.group(1) if match_secao else texto_limpo

        print(f"[DEBUG PDF] Executando busca de vértices no texto ({len(texto_busca)} caracteres)...")

        # 2. Regex de Extração Flexível (Sem catastrophic backtracking)
        # Mapeia: (Código do Vértice) (Longitude) (Latitude) (Altitude) (Código Vante) (Azimute) (Distância) (Confrontação)
        regex_sigef = re.compile(
            r'([A-Za-z0-9\-]+)\s+'
            r'(-?\d{1,3}°\d{1,2}[\'´]\d{1,2}(?:,\d{1,4})?["”]?|-?\d{1,3}[.,]\d{2,})\s+'
            r'(-?\d{1,3}°\d{1,2}[\'´]\d{1,2}(?:,\d{1,4})?["”]?|-?\d{1,3}[.,]\d{2,})\s+'
            r'(\d+(?:[.,]\d{1,3})?)\s+'
            r'([A-Za-z0-9\-]+)\s+'
            r'(\d{1,3}°\d{1,2}[\'´](?:\d{1,2}(?:,\d{1,4})?["”]?)?|\d{1,3}[.,]\d{2,})\s+'
            r'(\d+(?:[.,]\d{1,3})?)\s*'
            r'([^;,\n\r]*?)(?=[A-Za-z0-9\-]+\s+-?\d{1,3}(?:°|[.,])|$)'
        )

        try:
            for match in regex_sigef.finditer(texto_busca):
                cod_vertice = match.group(1).strip()
                if cod_vertice.lower() in ["vertice", "vértice", "codigo", "código", "ponto", "tipo"]:
                    continue

                conf = match.group(8).strip() if match.group(8) else ""
                conf = re.split(r'Este Memorial Descritivo|Página\s+\d+|CERTIFICAÇÃO:', conf, flags=re.IGNORECASE)[0].strip()

                novos_vertices.append(
                    Vertice(
                        projeto_id=projeto_id,
                        codigo_vertice=cod_vertice,
                        longitude=match.group(2).strip(),
                        latitude=match.group(3).strip(),
                        altitude=match.group(4).strip(),
                        codigo_segmento_vante=match.group(5).strip(),
                        azimute=match.group(6).strip(),
                        distancia=match.group(7).strip(),
                        confrontacao=conf,
                        origem="SIGEF"
                    )
                )
        except Exception as e:
            print(f"[ERRO REGEX] Falha na extração por regex: {e}")
            raise HTTPException(status_code=400, detail=f"Falha ao interpretar vértices no memorial PDF: {str(e)}")

        # 3. Fallback de Erro: se não encontrou vértices, lança HTTPException detalhada
        if len(novos_vertices) == 0:
            print("[AVISO PDF] Nenhum vértice identificado no PDF.")
            raise HTTPException(
                status_code=400,
                detail="Nenhum vértice ou coordenada foi identificado no memorial descritivo PDF. Verifique se o documento segue o formato padrão do SIGEF (tabela Descrição da Parcela)."
            )

    # Condição 2: Leitura de Planilha CSV (sigef_csv)
    else:
        texto_csv = None
        for enc in ["utf-8-sig", "utf-8", "iso-8859-1", "latin-1", "cp1252"]:
            try:
                texto_csv = conteudo_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue

        if texto_csv is None:
            texto_csv = conteudo_bytes.decode("utf-8", errors="ignore")

        linhas = [l.strip() for l in texto_csv.splitlines() if l.strip()]
        if not linhas:
            raise HTTPException(status_code=400, detail="O arquivo CSV enviado está vazio.")

        primeiras_linhas = linhas[:5]
        delimitador = ";" if any(";" in l for l in primeiras_linhas) else ","
        leitor = csv.reader(io.StringIO(texto_csv), delimiter=delimitador)

        cabecalhos_ignorados = [
            "vertice", "vértice", "codigo", "código", "longitude", "latitude",
            "altitude", "azimute", "distancia", "distância", "segmento", "vante", "coord"
        ]

        for row in leitor:
            if not row:
                continue
            colunas = [c.strip() for c in row]
            if not colunas or not colunas[0]:
                continue

            primeira_coluna_lower = colunas[0].lower()
            if any(termo in primeira_coluna_lower for termo in cabecalhos_ignorados):
                continue

            novos_vertices.append(
                Vertice(
                    projeto_id=projeto_id,
                    codigo_vertice=colunas[0] if len(colunas) > 0 else "",
                    longitude=colunas[1] if len(colunas) > 1 else "",
                    latitude=colunas[2] if len(colunas) > 2 else "",
                    altitude=colunas[3] if len(colunas) > 3 else "",
                    codigo_segmento_vante=colunas[4] if len(colunas) > 4 else "",
                    azimute=colunas[5] if len(colunas) > 5 else "",
                    distancia=colunas[6] if len(colunas) > 6 else "",
                    confrontacao=colunas[7] if len(colunas) > 7 else "",
                    origem="SIGEF"
                )
            )

        match_area = re.search(
            r'(?:área|area)(?:\s+total)?(?:\s+\(.*?local.*?\))?(?:\s+\(.*?\))?\s*[:=;]?\s*([\d]+(?:[.,]\d+)?)\s*(?:ha|hectares?)?',
            texto_csv,
            re.IGNORECASE
        )
        if match_area:
            try:
                area_calculada = float(match_area.group(1).replace(',', '.'))
                print(f"[DEBUG CSV] Área calculada identificada: {area_calculada} ha")
            except ValueError:
                pass

    if not novos_vertices:
        raise HTTPException(
            status_code=400,
            detail="Nenhuma coordenada válida foi identificada no documento SIGEF (CSV/PDF)."
        )

    # Exclui os vértices SIGEF antigos do projeto para evitar duplicações sem apagar dados RTK
    db.query(Vertice).filter(
        Vertice.projeto_id == projeto_id,
        (Vertice.origem == "SIGEF") | (Vertice.origem.is_(None))
    ).delete()

    # Atualiza a área total do projeto se uma área válida foi extraída (sem fallback estático)
    if area_calculada and area_calculada > 0:
        if projeto:
            projeto.area_hectares = area_calculada
            db.add(projeto)

    try:
        # Insere as novas linhas no banco de dados e salva a atualização do projeto
        db.bulk_save_objects(novos_vertices)
        db.commit()
        db.refresh(projeto)
    except Exception as e:
        db.rollback()
        print(f"[ERRO BANCO] Falha ao persistir vértices: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Erro ao salvar vértices no banco de dados: {str(e)}"
        )

    disparar_log_auditoria(request, usuario_id, f"processou_sigef_projeto_{projeto_id}_{len(novos_vertices)}_vertices_tipo_{documento.tipo}")

    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return RedirectResponse(url=f"/projetos/{projeto_id}?msg=sigef_processado_sucesso", status_code=303)

    return {
        "status": "sucesso",
        "mensagem": f"Documento SIGEF ({documento.tipo}) processado com sucesso.",
        "tipo_documento": documento.tipo,
        "total_vertices": len(novos_vertices),
        "area_hectares": projeto.area_hectares,
        "redirect_url": f"/projetos/{projeto_id}?msg=sigef_processado_sucesso"
    }


# ==========================================
# PROCESSAMENTO DE DADOS BRUTOS RTK (.TXT / .CSV)
# ==========================================
@app.post("/projetos/{projeto_id}/processar-rtk")
async def processar_rtk(projeto_id: int, request: Request, db: Session = Depends(get_db)):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id and "authorization" in request.headers:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            try:
                from jose import jwt
                payload = jwt.decode(token, os.getenv("SECRET_KEY", ""), algorithms=["HS256"])
                usuario_id = payload.get("usuario_id") or payload.get("sub")
            except Exception:
                pass

    if not usuario_id:
        raise HTTPException(status_code=401, detail="Não autorizado. Faça login primeiro.")

    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if not projeto:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")

    # 1. Buscar o último documento de dados brutos vinculado ao projeto
    documento = (
        db.query(Documento)
        .filter(Documento.projeto_id == projeto_id, Documento.tipo == "dados_brutos")
        .order_by(Documento.upload_em.desc())
        .first()
    )

    if not documento:
        # Fallback: busca arquivo .txt ou com 'rtk' no nome
        documento = (
            db.query(Documento)
            .filter(
                Documento.projeto_id == projeto_id,
                (Documento.nome_arquivo.ilike("%.txt")) | (Documento.nome_arquivo.ilike("%rtk%"))
            )
            .order_by(Documento.upload_em.desc())
            .first()
        )

    if not documento:
        raise HTTPException(
            status_code=404,
            detail="Nenhum arquivo de dados brutos RTK (.txt ou .csv) encontrado para este projeto. Faça o upload antes de processar."
        )

    # 2. Download do arquivo no MinIO
    try:
        minio_response = minio_client.get_object(MINIO_BUCKET_NAME, documento.minio_object_name)
        conteudo_bytes = minio_response.read()
        minio_response.close()
        minio_response.release_conn()
    except S3Error as e:
        print(f"[ERRO MINIO] Falha ao recuperar arquivo RTK: {e}")
        raise HTTPException(status_code=500, detail="Erro ao acessar o arquivo no servidor de armazenamento.")
    except Exception as e:
        print(f"[ERRO DOWNLOAD] Erro inesperado ao baixar do MinIO: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao recuperar arquivo do MinIO: {str(e)}")

    # 3. Decodificação do texto com múltiplos encodings
    texto_raw = None
    for enc in ["utf-8-sig", "utf-8", "iso-8859-1", "latin-1", "cp1252"]:
        try:
            texto_raw = conteudo_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if texto_raw is None:
        texto_raw = conteudo_bytes.decode("utf-8", errors="ignore")

    linhas = [l.strip() for l in texto_raw.splitlines() if l.strip()]
    if not linhas:
        raise HTTPException(status_code=400, detail="O arquivo de dados brutos RTK enviado está vazio.")

    # 4. Detecção de delimitador (vírgula, ponto e vírgula ou tabulação)
    primeiras_linhas = linhas[:5]
    delimitador = ";" if any(";" in l for l in primeiras_linhas) else ("," if any("," in l for l in primeiras_linhas) else "\t")
    leitor = csv.reader(io.StringIO(texto_raw), delimiter=delimitador)
    linhas_csv = [row for row in leitor if row and any(c.strip() for c in row)]

    if not linhas_csv:
        raise HTTPException(status_code=400, detail="Não foi possível estruturar as linhas do arquivo RTK.")

    # 5. Normalização do cabeçalho (minúsculas, sem acentos)
    import unicodedata

    def normalizar(termo: str) -> str:
        if not termo:
            return ""
        nfkd = unicodedata.normalize('NFKD', termo)
        sem_acento = "".join([c for c in nfkd if not unicodedata.combining(c)])
        return sem_acento.lower().strip()

    cabecalho_bruto = linhas_csv[0]
    cabecalho = [normalizar(col) for col in cabecalho_bruto]

    # 6. Dicionários de mapeamento heurístico de colunas
    heuristica = {
        "ponto": ['nome', 'ponto', 'id', 'name', 'pt'],
        "norte_lat": ['norte', 'n', 'lat', 'latitude', 'y'],
        "este_lon": ['este', 'e', 'lon', 'longitude', 'x'],
        "cota": ['cota', 'alt', 'altitude', 'z', 'elev'],
        "desc": ['desc', 'codigo', 'cod', 'code']
    }

    idx_ponto = -1
    idx_norte_lat = -1
    idx_este_lon = -1
    idx_cota = -1
    idx_desc = -1

    for i, col in enumerate(cabecalho):
        tokens = re.split(r'[^a-z0-9]+', col)

        # Mapeamento do Ponto
        if idx_ponto == -1 and any(p == col or p in tokens or (len(p) > 2 and p in col) for p in heuristica["ponto"]):
            idx_ponto = i
            continue

        # Mapeamento do Norte / Lat
        if idx_norte_lat == -1 and any(n == col or n in tokens or (len(n) > 2 and n in col) for n in heuristica["norte_lat"]):
            idx_norte_lat = i
            continue

        # Mapeamento do Este / Lon
        if idx_este_lon == -1 and any(e == col or e in tokens or (len(e) > 2 and e in col) for e in heuristica["este_lon"]):
            idx_este_lon = i
            continue

        # Mapeamento da Cota / Altitude
        if idx_cota == -1 and any(c == col or c in tokens or (len(c) > 2 and c in col) for c in heuristica["cota"]):
            idx_cota = i
            continue

        # Mapeamento da Descrição / Código
        if idx_desc == -1 and any(d == col or d in tokens or (len(d) > 2 and d in col) for d in heuristica["desc"]):
            idx_desc = i
            continue

    # 7. Validação das colunas obrigatórias de coordenadas
    if idx_norte_lat == -1 or idx_este_lon == -1:
        raise HTTPException(
            status_code=400,
            detail="Formato de colunas não reconhecido. Certifique-se de que o ficheiro tem cabeçalho."
        )

    # 8. Iteração sobre as linhas de dados usando os índices encontrados
    novos_vertices = []
    linhas_dados = linhas_csv[1:]

    for idx, row in enumerate(linhas_dados):
        if not row or not any(row):
            continue

        pt_val = row[idx_ponto].strip() if 0 <= idx_ponto < len(row) and row[idx_ponto].strip() else f"P{idx+1}"
        norte_val = row[idx_norte_lat].strip() if 0 <= idx_norte_lat < len(row) else ""
        este_val = row[idx_este_lon].strip() if 0 <= idx_este_lon < len(row) else ""
        cota_val = row[idx_cota].strip() if 0 <= idx_cota < len(row) else ""
        desc_val = row[idx_desc].strip() if 0 <= idx_desc < len(row) else ""

        if not norte_val and not este_val:
            continue

        novos_vertices.append(
            Vertice(
                projeto_id=projeto_id,
                codigo_vertice=pt_val,
                latitude=norte_val,    # Mapeia valor do idx_norte_lat (Norte ou Lat)
                longitude=este_val,   # Mapeia valor do idx_este_lon (Este ou Lon)
                altitude=cota_val,     # Mapeia valor do idx_cota
                codigo_segmento_vante="",
                azimute="",
                distancia="",
                confrontacao=desc_val, # Mapeia valor do idx_desc
                origem="RTK"
            )
        )

    if not novos_vertices:
        raise HTTPException(
            status_code=400,
            detail="Nenhuma coordenada válida foi identificada nas linhas de dados do arquivo RTK."
        )

    # 9. Limpa vértices RTK anteriores para evitar duplicações
    db.query(Vertice).filter(Vertice.projeto_id == projeto_id, Vertice.origem == "RTK").delete()

    try:
        db.bulk_save_objects(novos_vertices)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[ERRO BANCO RTK] Falha ao persistir vértices RTK: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Erro ao salvar pontos RTK no banco de dados: {str(e)}"
        )

    disparar_log_auditoria(request, usuario_id, f"processou_rtk_projeto_{projeto_id}_{len(novos_vertices)}_pontos")

    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return {
            "status": "sucesso",
            "mensagem": f"Dados brutos RTK processados com sucesso ({len(novos_vertices)} pontos identificados).",
            "total_vertices": len(novos_vertices),
            "origem": "RTK",
            "redirect_url": f"/projetos/{projeto_id}?msg=rtk_processado_sucesso"
        }

    return RedirectResponse(url=f"/projetos/{projeto_id}?msg=rtk_processado_sucesso", status_code=303)


@app.get("/auditoria", response_class=HTMLResponse)
def painel_auditoria(request: Request, db: Session = Depends(get_db)):
    usuario_id = request.session.get("usuario_id")
    role = request.session.get("role")

    # 1. Verifica se está logado
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)
        
    # ==========================================
    # 2. ENFORCEMENT RBAC (Exclusivo Admin)
    # ==========================================
    if role != "admin":
        disparar_log_auditoria(request, usuario_id, "tentativa_acesso_painel_auditoria_403")
        raise HTTPException(
            status_code=403, 
            detail="Acesso negado: Rota exclusiva para administradores do sistema."
        )

    # 3. Métricas de Negócio SaaS (Usuários Free vs Pro e MRR)
    qtd_free = db.query(Usuario).filter(Usuario.is_premium == False).count()
    qtd_pro = db.query(Usuario).filter(Usuario.is_premium == True).count()
    mrr = round(qtd_pro * 149.90, 2)

    # 4. Consome os dados do microsserviço de log
    try:
        resposta = requests.get(f"{LOG_URL}/logs?limite=100", timeout=5)
        logs = resposta.json().get("logs", []) if resposta.status_code == 200 else []
    except Exception as e:
        print(f"Erro ao buscar logs: {e}")
        logs = []

    # 5. Busca os nomes dos usuários para enriquecer os logs
    usuarios = db.query(Usuario).all()
    usuarios_db = {str(u.id): u.nome for u in usuarios}

    for log in logs:
        uid = log.get("usuario_id")
        log["nome_usuario"] = usuarios_db.get(uid, f"ID {uid}")

    usuario_atual = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    nome_admin = usuario_atual.nome if usuario_atual else "Admin"

    return templates.TemplateResponse(
        request, 
        "auditoria.html", 
        {
            "logs": logs,
            "role": role,
            "nome_usuario": nome_admin,
            "qtd_free": qtd_free,
            "qtd_pro": qtd_pro,
            "mrr": mrr
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
    ".txt": "dados_brutos",
}

async def processar_upload_documento(
    projeto_id: int,
    request: Request,
    file: UploadFile,
    db: Session,
    tipo_documento: Optional[str] = None
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

    # Ajuste e suporte refinado para dados brutos RTK
    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if tipo_documento in ["dados_brutos", "sigef_csv", "sigef_xlsx", "croqui_dwg", "croqui_dxf", "memorial_pdf"]:
        tipo = tipo_documento
    elif ext == ".txt":
        tipo = "dados_brutos"
    elif ext == ".csv":
        nome_lower = file.filename.lower()
        if "rtk" in nome_lower or "bruto" in nome_lower or "raw" in nome_lower or (projeto and projeto.status == "campo"):
            tipo = "dados_brutos"

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
    tipo_documento: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Upload de documento via path parameter (ex: /projetos/1/upload)."""
    return await processar_upload_documento(projeto_id, request, file, db, tipo_documento=tipo_documento)


@app.post("/documentos/upload")
async def upload_documento_form(
    request: Request,
    projeto_id: int = Form(...),
    file: UploadFile = File(...),
    tipo_documento: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """Upload de documento via multipart/form-data."""
    resultado = await processar_upload_documento(projeto_id, request, file, db, tipo_documento=tipo_documento)
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return RedirectResponse(url=f"/projetos/{projeto_id}?msg=upload_sucesso", status_code=303)
    return resultado


@app.post("/documentos/{doc_id}/excluir")
def excluir_documento(
    doc_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """Exclui o documento fisicamente do MinIO e remove o registro do banco de dados."""
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)

    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        return RedirectResponse(url="/dashboard?msg=documento_nao_encontrado", status_code=303)

    projeto_id = documento.projeto_id

    # 1. Apaga o arquivo fisicamente do MinIO
    try:
        minio_client.remove_object(MINIO_BUCKET_NAME, documento.minio_object_name)
    except S3Error as e:
        print(f"[ERRO MINIO] Falha ao remover documento {documento.minio_object_name}: {e}")
    except Exception as e:
        print(f"[ERRO STORAGE] Erro inesperado ao excluir do MinIO: {e}")

    # 2. Apaga o registro no MariaDB
    db.delete(documento)
    db.commit()

    disparar_log_auditoria(request, usuario_id, f"excluiu_documento_{doc_id}_projeto_{projeto_id}")

    return RedirectResponse(url=f"/projetos/{projeto_id}?msg=documento_excluido", status_code=303)


@app.get("/documentos/{doc_id}/download")
def baixar_documento(
    doc_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """Realiza o download de um documento anexado a partir do MinIO Storage."""
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return RedirectResponse(url="/login", status_code=303)

    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento não encontrado.")

    try:
        resposta = minio_client.get_object(MINIO_BUCKET_NAME, documento.minio_object_name)
    except S3Error as e:
        print(f"[ERRO MINIO] Falha ao recuperar arquivo para download {documento.minio_object_name}: {e}")
        raise HTTPException(status_code=500, detail="Erro ao obter arquivo no servidor de storage.")

    return StreamingResponse(
        resposta.stream(32 * 1024),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{documento.nome_arquivo}"'}
    )


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

    # 1. Busca os vértices do projeto no banco de dados
    vertices_db = db.query(Vertice).filter(Vertice.projeto_id == projeto_id).all()

    if not vertices_db:
        raise HTTPException(
            status_code=400,
            detail="Nenhum vértice ou ponto encontrado no banco para este projeto. Processe os dados brutos RTK ou o documento SIGEF antes de gerar o croqui."
        )

    # 2. Suporte inteligente a ambas as origens ('SIGEF' ou 'RTK'):
    # Se houver vértices de ambas as origens, prioriza conforme o status do projeto
    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if projeto and projeto.status == "campo":
        pontos_rtk = [v for v in vertices_db if getattr(v, "origem", None) == "RTK"]
        if pontos_rtk:
            vertices_db = pontos_rtk
    else:
        pontos_sigef = [v for v in vertices_db if getattr(v, "origem", None) == "SIGEF"]
        if pontos_sigef:
            vertices_db = pontos_sigef

    # 3. Lógica Cartesiana: Longitude para o eixo X e Latitude para o eixo Y
    # Mapeia diretamente os pontos (sejam RTK ou SIGEF) em coordenadas X/Y para o CAD
    pontos_dxf = [
        {
            "vertice": v.codigo_vertice or f"V{idx+1}",
            "x": v.longitude,  # Eixo X cartesiano (Longitude / Este)
            "y": v.latitude,   # Eixo Y cartesiano (Latitude / Norte)
            "codigo_vertice": v.codigo_vertice or f"V{idx+1}",
            "longitude": v.longitude,
            "latitude": v.latitude,
            "altitude": v.altitude,
            "origem": getattr(v, "origem", "SIGEF")
        }
        for idx, v in enumerate(vertices_db)
    ]

    nome_arquivo_dxf = f"croqui_projeto_{projeto_id}.dxf"
    minio_object_name = f"projeto_{projeto_id}_croqui_{uuid.uuid4().hex}.dxf"

    caminho_local_dxf = None
    try:
        # Gera o arquivo DXF local temporário a partir dos pontos cartesianos
        caminho_local_dxf = gerar_croqui_dxf(nome_arquivo_dxf, pontos_dxf)

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

    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return RedirectResponse(url=f"/projetos/{projeto_id}?msg=croqui_gerado", status_code=303)

    return {
        "status": "sucesso",
        "mensagem": "Croqui DXF gerado e armazenado com sucesso",
        "tipo_documento": "croqui_dxf",
        "redirect_url": f"/projetos/{projeto_id}?msg=croqui_gerado",
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
# ROTA DE COORDENADAS PARA VISUALIZAÇÃO NO MAPA (LEAFLET)
# ==========================================
@app.get("/projetos/{projeto_id}/coordenadas")
async def obter_coordenadas_projeto(
    projeto_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id and "authorization" in request.headers:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            try:
                from jose import jwt
                payload = jwt.decode(token, os.getenv("SECRET_KEY", ""), algorithms=["HS256"])
                usuario_id = payload.get("usuario_id") or payload.get("sub")
            except Exception:
                pass

    if not usuario_id:
        raise HTTPException(status_code=401, detail="Não autorizado")

    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if not projeto:
        raise HTTPException(status_code=404, detail="Projeto não encontrado")

    vertices_db = (
        db.query(Vertice)
        .filter(Vertice.projeto_id == projeto_id)
        .order_by(Vertice.id.asc())
        .all()
    )

    # Se estiver em fase de campo, prioriza vértices RTK caso existam
    if projeto.status == "campo":
        pontos_rtk = [v for v in vertices_db if getattr(v, "origem", None) == "RTK"]
        if pontos_rtk:
            vertices_db = pontos_rtk
    else:
        pontos_sigef = [v for v in vertices_db if getattr(v, "origem", None) == "SIGEF"]
        if pontos_sigef:
            vertices_db = pontos_sigef

    pontos = []
    for v in vertices_db:
        if not v.latitude or not v.longitude:
            continue

        lat_conv = _converter_coord(v.latitude, default=None)
        lng_conv = _converter_coord(v.longitude, default=None)

        pontos.append({
            "lat": lat_conv if lat_conv is not None else v.latitude,
            "lng": lng_conv if lng_conv is not None else v.longitude,
            "nome": v.codigo_vertice or "",
            "desc": v.confrontacao or ""
        })

    return {"pontos": pontos}


# ==========================================
# GERAÇÃO DE MEMORIAL DESCRITIVO (DOCXTPL)
# ==========================================
@app.post("/projetos/{projeto_id}/gerar-memorial")
async def gerar_memorial_projeto(
    projeto_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    # Verificação de Autenticação e RBAC
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
            detail="Acesso negado: apenas técnicos ou administradores podem gerar memoriais."
        )

    projeto = db.query(Projeto).filter(Projeto.id == projeto_id).first()
    if not projeto:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")

    cliente = projeto.cliente
    if not cliente and projeto.cliente_id:
        cliente = db.query(Cliente).filter(Cliente.id == projeto.cliente_id).first()

    vertices_db = db.query(Vertice).filter(Vertice.projeto_id == projeto_id).all()
    if not vertices_db:
        raise HTTPException(
            status_code=400,
            detail="Nenhum vértice encontrado no banco para este projeto. Processe um documento SIGEF antes de gerar o memorial."
        )

    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()

    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="O modelo deve ser um arquivo Word (.docx).")

    tmp_in_path = None
    tmp_out_path = None
    try:
        # Salva o arquivo modelo submetido temporariamente no disco
        conteudo_bytes = await file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp_in:
            tmp_in.write(conteudo_bytes)
            tmp_in_path = tmp_in.name

        context = {
            "imovel": {
                "denominacao": projeto.titulo, 
                "proprietario": cliente.nome_razao if cliente else "A definir", 
                "area_total": projeto.area_hectares,
                "municipio": "A definir pelo usuário", # Ou adicione campos no BD futuro
                "matricula": "A definir",
                "perimetro": "A calcular" # Será iterado da tabela de vértices no futuro
            },
            "poligonal": [{"confrontante": v.confrontacao, "azimute": v.azimute, "distancia": v.distancia, "vertice_destino": v.codigo_segmento_vante, "longitude": v.longitude, "latitude": v.latitude, "altitude": v.altitude} for v in vertices_db],
            "rt": {"nome_completo": usuario.nome if usuario else "Eng. Responsável", "numero_registro": "A preencher", "codigo_incra": "A preencher", "cpf": "A preencher"}
        }

        # Renderiza o modelo Word utilizando docxtpl
        doc = DocxTemplate(tmp_in_path)
        doc.render(context)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp_out:
            tmp_out_path = tmp_out.name
        doc.save(tmp_out_path)

        nome_arquivo_docx = f"memorial_descritivo_{projeto_id}.docx"
        minio_object_name = f"projeto_{projeto_id}_memorial_{uuid.uuid4().hex}.docx"

        minio_client.fput_object(
            MINIO_BUCKET_NAME,
            minio_object_name,
            tmp_out_path,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    except Exception as e:
        print(f"[ERRO MEMORIAL DOCX] Falha na geração do memorial: {e}")
        raise HTTPException(status_code=400, detail=f"Erro ao processar template do memorial Word: {str(e)}")
    finally:
        if tmp_in_path and os.path.exists(tmp_in_path):
            try:
                os.remove(tmp_in_path)
            except OSError:
                pass
        if tmp_out_path and os.path.exists(tmp_out_path):
            try:
                os.remove(tmp_out_path)
            except OSError:
                pass

    documento = Documento(
        projeto_id=projeto_id,
        nome_arquivo=nome_arquivo_docx,
        minio_object_name=minio_object_name,
        tipo="memorial_docx"
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    disparar_log_auditoria(request, usuario_id, f"gerou_memorial_docx_projeto_{projeto_id}")

    return {
        "status": "sucesso",
        "mensagem": "Memorial descritivo DOCX gerado e armazenado com sucesso",
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
    por debaixo dos panos quando o pagamento é aprovado, cancelado ou falha.
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

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Evento de Assinatura Ativa (Checkout realizado com sucesso)
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            usuario_id = session.get("client_reference_id")
            customer_id = session.get("customer")

            if usuario_id:
                cursor.execute(
                    "UPDATE usuarios SET is_premium = TRUE, stripe_customer_id = %s WHERE id = %s",
                    (customer_id, usuario_id)
                )
                conn.commit()
                print(f"Sucesso! Usuário {usuario_id} agora é PREMIUM (Customer: {customer_id}).")

        # 2. Eventos de Cancelamento ou Falha de Pagamento (Rebaixa para Free)
        elif event['type'] in ['customer.subscription.deleted', 'invoice.payment_failed']:
            data_object = event['data']['object']
            customer_id = data_object.get("customer")

            if customer_id:
                cursor.execute(
                    "UPDATE usuarios SET is_premium = FALSE WHERE stripe_customer_id = %s",
                    (customer_id,)
                )
                conn.commit()
                print(f"Evento '{event['type']}' processado: Usuário com Stripe Customer '{customer_id}' rebaixado para plano FREE.")

    except Exception as e:
        print(f"Erro ao processar evento no webhook: {e}")
    finally:
        cursor.close()
        conn.close()

    # O Stripe espera um status 200 para saber que recebemos a mensagem
    return {"status": "sucesso"}
