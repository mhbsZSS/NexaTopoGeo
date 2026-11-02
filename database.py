import os
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# load_dotenv() puxa as variáveis do arquivo .env para a memória
load_dotenv()

DB_HOST = os.getenv("DB_HOST", "db")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "tomflix_db")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"mysql+mysqlconnector://{DB_USER}:{DB_PASSWORD}@{DB_HOST}/{DB_NAME}"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dependência para injetar a sessão do banco SQLAlchemy nas rotas FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_connection():
    """Cria e retorna uma conexão segura com o banco de dados (legado mysql.connector)."""
    try:
        connection = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Erro crítico ao conectar ao MySQL: {e}")
        return None
