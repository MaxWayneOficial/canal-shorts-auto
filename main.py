
import os
import json
import random
import time
import re
import asyncio
import subprocess
import textwrap
from pathlib import Path
from html import unescape

import requests
from PIL import Image, ImageDraw, ImageFont
import edge_tts

# CONFIGURACIÓN
OUT = Path("output")
OUT.mkdir(exist_ok=True)

W, H = 1080, 1920
API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {
    "User-Agent": "ShortsGenerator/1.0 (automated video generator)"
}

GEMINI_MODEL = "gemini-3.8-flash"

NICHES = [
    "ciencia cotidiana",
    "historia sorprendente",
    "tecnología",
    "animales sorprendentes",
    "curiosidades del mundo"
]


# GENERAR GUION CON GEMINI
def generate_script():
    key = os.environ.get("GEMINI_API_KEY")

    if not key:
        raise RuntimeError(
            "Falta configurar el secreto GEMINI_API_KEY en GitHub."
        )

    niche = random.choice(NICHES)

    prompt = f"""
Crea un guion original para YouTube Shorts en español latino.
Tema: {niche}.

Devuelve exclusivamente un objeto JSON válido, sin Markdown:
"title": título atractivo de máximo 65 caracteres.
"description": descripción breve.
"narration": narración de 90 a 110 palabras.
"scenes": exactamente 5 objetos con:
"search_term": búsqueda visual concreta en inglés.
"caption": frase breve en español para pantalla.

Usa información verificable.
No inventes cifras, fechas ni hechos.
No incluyas consejos peligrosos ni afirmaciones médicas.
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_MODEL
        + ":generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    response = None

    # REINTENTOS AUTOMÁTICOS
    for intento in range(4):
        try:
            response = requests.post(
                url,
                headers={"x-goog-api-key": key},
                json=payload,
                timeout=(20, 180)
            )

            if response.status_code in (429, 500, 502, 503, 504):
                if intento == 3:
                    response.raise_for_status()

                espera = min(15 * (2 ** intento), 90)

                print(
                    f"Gemini respondió {response.status_code}. "
                    f"Reintentando en {espera} segundos..."
                )

                time.sleep(espera)
                continue

            response.raise_for_status()
            break

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError
        ):
            if intento == 3:
                raise

            espera = min(10 * (2 ** intento), 60)

            print(
                f"Gemini tardó demasiado. "
                f"Reintento {intento + 2}/4..."
            )

            time.sleep(espera)

    result = response.json()

    try:
        raw = result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            "Gemini no devolvió un guion válido: "
            + json.dumps(result, ensure_ascii=False)[:1500]
        )

    raw = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$",
        "",
        raw.strip(),
        flags=re.IGNORECASE
    )

    data = json.loads(raw)

    required = [
        "title",
        "description",
        "narration",
        "scenes"
    ]

    for field in required:
        if field not in data:
            raise ValueError(
                f"Falta el campo obligatorio: {field}"
            )

    if not isinstance(data["scenes"], list):
        raise ValueError("Las escenas no tienen formato de lista.")

    if len(data["scenes"]) != 5:
        raise ValueError(
            "El guion debe contener exactamente cinco escenas."
        )

    for scene in data["scenes"]:
        if not scene.get("search_term") or not scene.get("caption"):
            raise ValueError(
                "Una escena no contiene search_term o caption."
            )

    return data, niche


# BUSCAR IMÁGENES EN WIKIMEDIA COMMONS
def find_image(term, index):

    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": term,
        "gsrnamespace": 6,
        "gsrlimit": 20,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": 1400
    }

    response = requests.get(
        API,
        params=params,
        headers=HEADERS,
        timeout=(15, 60)
    )

    response.raise_for_status()

    pages = list(
        response.json()
        .get("query", {})
        .get("pages", {})
        .values()
    )

    choices = []

    for page in pages:

        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})

        license_name = (
            meta.get("LicenseShortName", {})
            .get("value", "")
        )

        mime = info.get("mime", "")

        if (
            info.get("thumburl")
            and license_name
            and mime.startswith("image/")
        ):
            choices.append((page, info, meta))

    if not choices:
        raise RuntimeError(
            "Wikimedia Commons no encontró una imagen "
            "con licencia identificable para: " + term
        )

    page, info, meta = random.choice(choices)

    download = requests.get(
        info["thumburl"],
        headers=HEADERS,
        timeout=(15, 90)
    )

    download.raise_for_status()

    path = OUT / f"imagen{index}.jpg"
    path.write_bytes(download.content)

    def clean(value):
        return unescape(
            re.sub(
                r"<[^>]*>",
                "",
                str(value or "")
            )
        ).strip()

    credit = {
        "archivo": page.get("title", ""),
        "autor": clean(
            meta.get("Artist", {}).get("value", "")
        ),
        "licencia": clean(license_name),
        "fuente": info.get("descriptionurl", "")
    }

    if not credit["fuente"]:
        credit["fuente"] = "https://commons.wikimedia.org/"

    return path, credit


# GENERAR VOZ EN ESPAÑOL
async def generate_voice(text):

    communicator = edge_tts.Communicate(
        text,
        "es-AR-TomasNeural",
        rate="+3%"
    )

    await communicator.save(
        str(OUT / "voz.mp3")
    )


# CREAR VIDEO VERTICAL
def create_video(data, images):

    font_path = (
        "/usr/share/fonts/truetype/dejavu/"
        "DejaVuSans-Bold.ttf"
    )

    font = ImageFont.truetype(
        font_path,
        58
    )

    segments = []

    for i, (image_path, caption) in enumerate(images):

        overlay = Image.new(
            "RGBA",
            (W, H),
            (0, 0, 0, 0)
        )

        draw = ImageDraw.Draw(overlay)

        lines = textwrap.wrap(
            caption,
            22
        ) or [caption]

        y = H - 430 - len(lines) * 75

        draw.rounded_rectangle(
            (
                55,
                y - 25,
                W - 55,
                y + len(lines) * 75 + 25
            ),
            radius=25,
            fill=(0, 0, 0, 195)
        )

        for n, line in enumerate(lines):

            draw.text(
                (85, y + n * 75),
                line,
                font=font,
                fill="white",
                stroke_width=2,
                stroke_fill="black"
            )

        overlay_path = OUT / f"texto{i}.png"
        overlay.save(overlay_path)

        segment = OUT / f"parte{i}.mp4"

        filter_complex = (
            f"[0:v]scale={W}:{H}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={W}:{H},"
            f"zoompan=z='min(zoom+0.0008,1.08)':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d=240:s={W}x{H}:fps=30[v];"
            f"[v][1:v]overlay=0:0,format=yuv420p[out]"
        )

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loop", "1",
                "-framerate", "30",
                "-i", str(image_path),
                "-i", str(overlay_path),
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-t", "8",
                "-an",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-r", "30",
                str(segment)
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )

        segments.append(segment)

    # UNIR LAS CINCO ESCENAS
    concat_file = OUT / "lista.txt"

    concat_file.write_text(
        "".join(
            f"file '{segment.resolve()}'\n"
            for segment in segments
        ),
        encoding="utf-8"
    )

    joined = OUT / "video.mp4"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            str(joined)
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )

    # GENERAR NARRACIÓN
    asyncio.run(
        generate_voice(data["narration"])
    )

    # VIDEO FINAL CON AUDIO
    final_video = OUT / "short.mp4"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i", str(joined),
            "-i", str(OUT / "voz.mp3"),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(final_video)
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )

    return final_video


# PROGRAMA PRINCIPAL
if __name__ == "__main__":

    print("Iniciando generador de YouTube Shorts...")

    data, niche = generate_script()

    print("Temática elegida:", niche)
    print("Título:", data["title"])

    images = []
    credits = []

    for index, scene in enumerate(data["scenes"]):

        print(
            f"Buscando imagen {index + 1}/5: "
            + scene["search_term"]
        )

        image_path, credit = find_image(
            scene["search_term"],
            index
        )

        images.append(
            (image_path, scene["caption"])
        )

        credits.append(credit)

    print("Creando video...")

    final_video = create_video(
        data,
        images
    )

    # GUARDAR METADATOS
    metadata = {
        "niche": niche,
        **data,
        "media_credits": credits
    }

    (OUT / "metadata.json").write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    # GUARDAR CRÉDITOS DE LAS IMÁGENES
    attribution = [
        "Créditos de imágenes de Wikimedia Commons:"
    ]

    for item in credits:

        attribution.append(
            f"- {item['archivo']} | "
            f"Autor: {item['autor']} | "
            f"Licencia: {item['licencia']} | "
            f"Fuente: {item['fuente']}"
        )

    (OUT / "creditos.txt").write_text(
        "\n".join(attribution),
        encoding="utf-8"
    )

    print("SHORT GENERADO CORRECTAMENTE")
    print("Archivo:", final_video)
