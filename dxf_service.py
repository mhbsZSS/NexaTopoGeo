import os
import tempfile
import ezdxf


def gerar_croqui_dxf(nome_arquivo: str, pontos: list) -> str:
    """
    Gera um arquivo DXF contendo o polígono da propriedade e a identificação dos vértices.

    :param nome_arquivo: Nome do arquivo a ser gerado (ex: 'croqui_projeto_1.dxf')
    :param pontos: Lista de dicionários no formato [{'vertice': 'P1', 'x': 100.0, 'y': 100.0}, ...]
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

    # Extração das coordenadas 2D (x, y)
    coords = [(float(p["x"]), float(p["y"])) for p in pontos]

    # Desenha as linhas fechadas (LWPOLYLINE) formando o polígono da propriedade
    msp.add_lwpolyline(
        coords,
        close=True,
        dxfattribs={"layer": "POLIGONO"}
    )

    # Insere o texto identificador do vértice próximo a cada coordenada (offset)
    for p in pontos:
        vertice = str(p.get("vertice", ""))
        x = float(p["x"])
        y = float(p["y"])

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

