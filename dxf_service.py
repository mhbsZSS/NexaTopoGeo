import os
import tempfile
import re
import ezdxf


def _converter_coord(coord_valor, default=0.0) -> float:
    """Converte coordenadas em formatos diversos (graus DMS, float, string) para float."""
    if isinstance(coord_valor, (int, float)):
        return float(coord_valor)
    if not coord_valor:
        return default
    coord_str = str(coord_valor).strip()
    
    # Ex: -51°06'37,683" ou -21°51'04.660"
    m = re.match(r'^(-)?(\d+)°(?:(\d+)[\'´])?(?:(\d+(?:[.,]\d+)?)["”]?)?', coord_str)
    if m:
        sinal = -1.0 if m.group(1) else 1.0
        graus = float(m.group(2))
        minutos = float(m.group(3) or 0)
        segundos = float((m.group(4) or '0').replace(',', '.'))
        return sinal * (graus + minutos / 60.0 + segundos / 3600.0)

    try:
        return float(coord_str.replace(',', '.'))
    except ValueError:
        return default


def gerar_croqui_dxf(nome_arquivo: str, pontos: list) -> str:
    """
    Gera um arquivo DXF contendo o polígono da propriedade e a identificação dos vértices.

    :param nome_arquivo: Nome do arquivo a ser gerado (ex: 'croqui_projeto_1.dxf')
    :param pontos: Lista de modelos Vertice ou dicionários com coordenadas.
    :return: Caminho do arquivo local temporário gerado.
    """
    if not pontos:
        raise ValueError("A lista de coordenadas não pode estar vazia.")

    # Inicializa documento DXF versão AutoCAD 2010 (compatível com AutoCAD e QGIS)
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    # Criação de layers para o croqui topográfico
    if "POLIGONO" not in doc.layers:
        doc.layers.add(name="POLIGONO", color=1)  # Vermelho
    if "VERTICES_TEXTO" not in doc.layers:
        doc.layers.add(name="VERTICES_TEXTO", color=7)  # Branco/Preto

    pontos_processados = []
    for idx, p in enumerate(pontos):
        if hasattr(p, "codigo_vertice"):
            v_nome = p.codigo_vertice or f"V{idx+1}"
            x = _converter_coord(getattr(p, "longitude", None), default=float(idx * 50))
            y = _converter_coord(getattr(p, "latitude", None), default=float(idx * 50))
        elif isinstance(p, dict):
            v_nome = str(p.get("vertice", f"V{idx+1}"))
            x = _converter_coord(p.get("x", idx * 50))
            y = _converter_coord(p.get("y", idx * 50))
        else:
            v_nome = f"V{idx+1}"
            x = float(idx * 50)
            y = float(idx * 50)

        pontos_processados.append({"vertice": v_nome, "x": x, "y": y})

    # Extração das coordenadas 2D (x, y)
    coords = [(p["x"], p["y"]) for p in pontos_processados]

    # Desenha as linhas fechadas (LWPOLYLINE) formando o polígono da propriedade
    msp.add_lwpolyline(
        coords,
        close=True,
        dxfattribs={"layer": "POLIGONO"}
    )

    # Insere o texto identificador do vértice próximo a cada coordenada (offset)
    for p in pontos_processados:
        vertice = p["vertice"]
        x = p["x"]
        y = p["y"]

        msp.add_text(
            vertice,
            dxfattribs={
                "layer": "VERTICES_TEXTO",
                "height": 1.5,
                "insert": (x + 0.5, y + 0.5)
            }
        )

    # Diretório temporário e salvamento do arquivo DXF
    if not nome_arquivo.lower().endswith(".dxf"):
        nome_arquivo = f"{nome_arquivo}.dxf"

    temp_dir = tempfile.gettempdir()
    caminho_arquivo = os.path.join(temp_dir, nome_arquivo)
    doc.saveas(caminho_arquivo)

    return caminho_arquivo

