from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, func, Text
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    senha_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="usuario", nullable=False)
    is_premium = Column(Boolean, default=False, server_default="0", nullable=False)
    stripe_customer_id = Column(String(255), nullable=True)
    avatar_url = Column(String(500), nullable=True)
    bio = Column(String(500), nullable=True)

    # Relacionamentos
    clientes = relationship("Cliente", back_populates="usuario")
    projetos = relationship("Projeto", back_populates="tecnico", foreign_keys="Projeto.usuario_id_tecnico")

class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    senha_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="usuario", nullable=False)
    is_premium = Column(Boolean, default=False, server_default="0", nullable=False)
    stripe_customer_id = Column(String(255), nullable=True)
    avatar_url = Column(String(500), nullable=True)
    bio = Column(String(500), nullable=True)
    
    # --- NOVA COLUNA ADICIONADA AQUI ---
    reset_token = Column(String(100), nullable=True)

    # Relacionamentos
    clientes = relationship("Cliente", back_populates="usuario")
    projetos = relationship("Projeto", back_populates="tecnico", foreign_keys="Projeto.usuario_id_tecnico")


class Cliente(Base):
    __tablename__ = "clientes"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    nome_razao = Column(String(255), nullable=False)
    cpf_cnpj = Column(String(20), unique=True, index=True, nullable=False)
    telefone = Column(String(20), nullable=True)
    criado_em = Column(DateTime, default=func.now(), nullable=False)

    # Relacionamentos
    usuario = relationship("Usuario", back_populates="clientes")
    projetos = relationship("Projeto", back_populates="cliente", cascade="all, delete-orphan")


class Projeto(Base):
    __tablename__ = "projetos"

    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False)
    usuario_id_tecnico = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    titulo = Column(String(255), nullable=False)
    status = Column(String(50), default="orcamento", nullable=False)  # orcamento, campo, desenho, cartorio, concluido
    area_hectares = Column(Float, nullable=True)
    valor_orcamento = Column(Float, nullable=True)
    perimetro = Column(Float, nullable=True)
    criado_em = Column(DateTime, default=func.now(), nullable=False)

    # Relacionamentos
    cliente = relationship("Cliente", back_populates="projetos")
    tecnico = relationship("Usuario", back_populates="projetos", foreign_keys=[usuario_id_tecnico])
    documentos = relationship("Documento", back_populates="projeto", cascade="all, delete-orphan")
    vertices = relationship("Vertice", back_populates="projeto", cascade="all, delete-orphan")


class Documento(Base):
    __tablename__ = "documentos"

    id = Column(Integer, primary_key=True, index=True)
    projeto_id = Column(Integer, ForeignKey("projetos.id"), nullable=False)
    nome_arquivo = Column(String(255), nullable=False)
    minio_object_name = Column(String(255), unique=True, index=True, nullable=False)
    tipo = Column(String(50), nullable=False)  # sigef_csv, croqui_dxf, memorial_pdf
    upload_em = Column(DateTime, default=func.now(), nullable=False)

    # Relacionamentos
    projeto = relationship("Projeto", back_populates="documentos")


class Vertice(Base):
    __tablename__ = "vertices"

    id = Column(Integer, primary_key=True, index=True)
    projeto_id = Column(Integer, ForeignKey("projetos.id"), nullable=False)
    codigo_vertice = Column(String(50), nullable=True)
    longitude = Column(String(50), nullable=True)
    latitude = Column(String(50), nullable=True)
    altitude = Column(String(50), nullable=True)
    codigo_segmento_vante = Column(String(50), nullable=True)
    azimute = Column(String(50), nullable=True)
    distancia = Column(String(50), nullable=True)
    confrontacao = Column(Text, nullable=True)
    origem = Column(String(50), default="SIGEF")

    # Relacionamentos
    projeto = relationship("Projeto", back_populates="vertices")

# --- NOVA CLASSE ADICIONADA NO FINAL DO ARQUIVO ---
class LogAuditoria(Base):
    __tablename__ = "logs_auditoria"
    
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(String(50))
    acao = Column(String(255))
    ip_origem = Column(String(50))
    data_hora = Column(DateTime(timezone=True), server_default=func.now())
