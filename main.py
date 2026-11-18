import asyncio
import json
import os
import random
import re
import subprocess
import textwrap
import time
from html import unescape
from io import BytesIO
from pathlib import Path

import edge_tts
import requests
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# CONFIGURACION
# ============================================================
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WIDTH, HEIGHT = 1080, 1920
FPS = 30
SCENE_SECONDS = 9
SCENE_COUNT = 5
VIDEO_SECONDS = SCENE_SECONDS * SCENE_COUNT

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
VOICE = os.getenv("EDGE_TTS_VOICE", "es-AR-TomasNeural")

HEADERS = {
    "User-Agent": "CanalShortsAuto/2.0 (automated educational YouTube Shorts generator)"
}

NICHES = [
    "ciencia cotidiana",
    "historia sorprendente",
    "tecnologia",
    "animales sorprendentes",
    "curiosidades del mundo",
]

STOP_WORDS = {
    "underwater", "macro", "reef", "photo", "image", "picture",
    "closeup", "close", "high", "quality", "background", "cinematic",
    "beautiful", "detailed", "footage", "stock", "video", "the", "and",
    "with", "for", "from",
}

USED_IMAGE_TITLES = set()


def retry_delay(response, attempt):
    """Calcula una espera prudente para errores temporales del servidor."""
    if response is not None:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return min(max(float(value), 2), 90)
            except ValueError:
                pass
    return min(8 * (2 ** attempt), 60)


def get_with_retries(url, params=None, timeout=(15, 60), attempts=4):
    """GET con reintentos ante cortes temporales y errores HTTP 429/5xx."""
    last_error = None
    for attempt in range(attempts):
        response = None
        try:
            response = requests.get(
                url, params=params, headers=HEADERS, timeout=timeout
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == attempts - 1:
                    response.raise_for_status()
                wait = retry_delay(response, attempt)
                print(f"Wikimedia HTTP {response.status_code}; reintento en {wait:.0f}s.")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            wait = min(5 * (2 ** attempt), 30)
            print(f"Corte de conexión; reintento en {wait}s.")
            time.sleep(wait)
    raise RuntimeError(f"No se pudo conectar con Wikimedia Commons: {last_error}")


def generate_script():
    """Pide a Groq un tema, un guion breve y cinco escenas en JSON."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "FALTA GROQ_API_KEY. Configurá ese secreto en GitHub > Settings > Secrets and variables > Actions."
        )

    niche = random.choice(NICHES)
    system_prompt = (
        "Sos guionista de videos educativos cortos para YouTube Shorts. "
        "Escribí en español latino natural, claro y atractivo. "
        "No inventes datos, cifras ni fechas. Si un dato es dudoso, elegí otro tema. "
        "La respuesta debe ser exclusivamente un objeto JSON válido."
    )
    user_prompt = f"""
Creá un YouTube Short ORIGINAL sobre esta temática: {niche}.

Devolvé exclusivamente JSON, sin Markdown, con esta estructura exacta:
{{
  "title": "título atractivo de máximo 65 caracteres",
  "description": "descripción breve en español",
  "narration": "narración completa en español latino de 90 a 110 palabras",
  "scenes": [
    {{"search_term": "búsqueda concreta en inglés de 2 a 4 palabras", "caption": "texto breve en español"}}
  ]
}}

REGLAS:
- Incluí exactamente 5 escenas.
- La narración debe tener entre 90 y 110 palabras y sonar natural al hablar.
- Cada escena debe representar una parte de la narración y tener una imagen fácil de encontrar.
- search_term debe ser una búsqueda visual concreta en inglés, no una frase larga.
- caption debe tener como máximo 8 palabras.
- No uses llamadas a suscribirse, hashtags ni emojis dentro del JSON.
- Usá información general comprobable, sin afirmaciones médicas ni consejos peligrosos.
"""

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.6,
        "max_tokens": 1800,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    response = None
    for attempt in range(4):
        try:
            response = requests.post(
                GROQ_API_URL,
                headers=headers,
                json=payload,
                timeout=(20, 120),
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    raise RuntimeError(
                        f"Groq devolvió HTTP {response.status_code} después de 4 intentos: "
                        f"{response.text[:1800]}"
                    )
                wait = retry_delay(response, attempt)
                print(
                    f"Groq HTTP {response.status_code}; reintento "
                    f"{attempt + 2}/4 en {wait:.0f}s."
                )
                time.sleep(wait)
                continue

            if response.status_code in (401, 403):
                raise RuntimeError(
                    "Groq rechazó la clave (HTTP " + str(response.status_code) + "). "
                    "Revisá que GROQ_API_KEY sea la clave correcta y que siga activa."
                )
            if response.status_code == 400:
                raise RuntimeError(
                    "Groq rechazó el modelo o los parámetros (HTTP 400): "
                    + response.text[:2200]
                )
            response.raise_for_status()
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 3:
                raise RuntimeError(f"Groq no respondió después de 4 intentos: {exc}") from exc
            wait = min(8 * (2 ** attempt), 60)
            print(f"Sin respuesta de Groq; reintento {attempt + 2}/4 en {wait}s.")
            time.sleep(wait)

    if response is None or not response.ok:
        raise RuntimeError("Groq no entregó una respuesta válida.")

    try:
        result = response.json()
        raw = result["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        details = response.text[:1800] if response is not None else "sin respuesta"
        raise RuntimeError(f"Groq respondió con un formato inesperado: {details}") from exc

    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("Groq devolvió el contenido del guion vacío.")

    raw = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$",
        "",
        raw.strip(),
        flags=re.IGNORECASE,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Groq no devolvió JSON válido ({exc}). Respuesta: {raw[:1500]}"
        ) from exc

    for field in ("title", "description", "narration", "scenes"):
        if field not in data:
            raise ValueError(f"Falta el campo obligatorio '{field}' en el JSON de Groq.")
    if not all(isinstance(data[field], str) and data[field].strip()
               for field in ("title", "description", "narration")):
        raise ValueError("El título, la descripción y la narración deben ser textos no vacíos.")
    if not isinstance(data["scenes"], list) or len(data["scenes"]) != SCENE_COUNT:
        raise ValueError("Groq debe devolver exactamente cinco escenas.")

    word_count = len(data["narration"].split())
    if not 75 <= word_count <= 125:
        raise ValueError(
            f"La narración tiene {word_count} palabras; se esperaban entre 90 y 110."
        )

    for number, scene in enumerate(data["scenes"], start=1):
        if not isinstance(scene, dict):
            raise ValueError(f"La escena {number} no es un objeto JSON.")
        for field in ("search_term", "caption"):
            if not isinstance(scene.get(field), str) or not scene[field].strip():
                raise ValueError(f"La escena {number} no tiene '{field}' válido.")

    return data, niche


def build_search_terms(term):
    """Prepara búsquedas alternativas si el término inicial es muy específico."""
    words = re.findall(r"[a-zA-Z]{3,}", term.lower())
    useful = [word for word in words if word not in STOP_WORDS]
    terms = [term.strip()]
    if useful:
        terms.append(" ".join(useful))
        for index in range(len(useful) - 1):
            terms.append(" ".join(useful[index:index + 2]))
        terms.extend(reversed(useful))
    return list(dict.fromkeys(query for query in terms if query))


def clean_html(value):
    """Limpia etiquetas HTML de los metadatos de Wikimedia."""
    return unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def find_image(term, index):
    """Busca una imagen en Wikimedia Commons y conserva su atribución."""
    selected = None
    for query in build_search_terms(term):
        print(f"Buscando imagen {index + 1}/{SCENE_COUNT}: {query}")
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": 35,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime",
            "iiurlwidth": 1400,
            "iiextmetadatalanguage": "en",
        }
        payload = get_with_retries(COMMONS_API, params=params).json()
        if "error" in payload:
            raise RuntimeError(
                "Wikimedia devolvió un error: "
                + json.dumps(payload["error"], ensure_ascii=False)[:1200]
            )

        candidates = []
        pages = list(payload.get("query", {}).get("pages", {}).values())
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            metadata = info.get("extmetadata", {})
            mime_type = info.get("mime", "")
            media_url = info.get("thumburl") or info.get("url")
            license_name = (
                metadata.get("LicenseShortName", {}).get("value", "")
                or metadata.get("UsageTerms", {}).get("value", "")
            )
            title = page.get("title", "")
            if (media_url and mime_type.startswith("image/") and license_name
                    and title not in USED_IMAGE_TITLES):
                candidates.append((page, info, metadata, license_name))

        if candidates:
            selected = random.choice(candidates)
            print(f"Imagen encontrada con la búsqueda: {query}")
            break
        print(f"No hubo imágenes nuevas con licencia identificable para: {query}")

    if selected is None:
        raise RuntimeError(
            f"No se encontró una imagen con licencia para '{term}'. "
            "Probá otra ejecución o revisá Wikimedia Commons."
        )

    page, info, metadata, license_name = selected
    file_title = page.get("title", "")
    USED_IMAGE_TITLES.add(file_title)
    media_url = info.get("thumburl") or info.get("url")
    download = get_with_retries(media_url, timeout=(15, 90))

    try:
        image = Image.open(BytesIO(download.content))
        image.load()
        image = image.convert("RGB")
    except Exception as exc:
        raise RuntimeError(
            f"El archivo descargado de Wikimedia no se pudo leer para '{term}': {exc}"
        ) from exc

    image_path = OUTPUT_DIR / f"imagen_{index + 1}.jpg"
    image.save(image_path, format="JPEG", quality=91, optimize=True)

    credit = {
        "archivo": file_title,
        "autor": clean_html(metadata.get("Artist", {}).get("value", "")),
        "licencia": clean_html(license_name),
        "fuente": info.get("descriptionurl", "https://commons.wikimedia.org/"),
    }
    return image_path, credit


async def generate_voice(narration):
    """Genera la narración con la voz argentina de Edge TTS."""
    voice_file = OUTPUT_DIR / "voz.mp3"
    last_error = None
    for attempt in range(3):
        try:
            communicator = edge_tts.Communicate(narration, VOICE, rate="+3%")
            await communicator.save(str(voice_file))
            if not voice_file.exists() or voice_file.stat().st_size < 1000:
                raise RuntimeError("Edge TTS devolvió un archivo de audio vacío.")
            return voice_file
        except Exception as exc:
            last_error = exc
            if attempt == 2:
                break
            wait = 5 * (attempt + 1)
            print(f"Error generando la voz; reintento en {wait}s: {exc}")
            await asyncio.sleep(wait)
    raise RuntimeError(f"No se pudo generar la voz con Edge TTS: {last_error}")


def run_ffmpeg(command, stage):
    """Ejecuta FFmpeg y devuelve un mensaje útil si falla."""
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "Sin detalles")[-5000:]
        raise RuntimeError(
            f"FFmpeg falló durante '{stage}' (código {result.returncode}):\n{details}"
        )


def create_video(data, images):
    """Crea cinco escenas verticales, las une y agrega la narración."""
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not Path(font_path).exists():
        raise RuntimeError(f"No se encontró la fuente necesaria: {font_path}")

    font = ImageFont.truetype(font_path, 58)
    segments = []

    for index, (image_path, caption) in enumerate(images):
        print(f"Renderizando escena {index + 1}/{SCENE_COUNT}...")
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        lines = textwrap.wrap(caption, 22) or [caption]
        y = HEIGHT - 430 - len(lines) * 75
        box_bottom = y + len(lines) * 75 + 25
        draw.rounded_rectangle(
            (55, y - 25, WIDTH - 55, box_bottom),
            radius=25,
            fill=(0, 0, 0, 195),
        )
        for line_number, line in enumerate(lines):
            draw.text(
                (85, y + line_number * 75),
                line,
                font=font,
                fill="white",
                stroke_width=2,
                stroke_fill="black",
            )

        overlay_path = OUTPUT_DIR / f"texto_{index + 1}.png"
        overlay.save(overlay_path)
        segment_path = OUTPUT_DIR / f"escena_{index + 1}.mp4"

        filter_complex = (
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},"
            f"zoompan=z='min(zoom+0.0008,1.08)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={SCENE_SECONDS * FPS}:s={WIDTH}x{HEIGHT}:fps={FPS}[base];"
            f"[base][1:v]overlay=0:0:shortest=1,format=yuv420p[out]"
        )
        run_ffmpeg(
            [
                "ffmpeg", "-y",
                "-loop", "1", "-framerate", str(FPS), "-i", str(image_path),
                "-loop", "1", "-framerate", str(FPS), "-i", str(overlay_path),
                "-filter_complex", filter_complex,
                "-map", "[out]", "-t", str(SCENE_SECONDS), "-an",
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-r", str(FPS),
                str(segment_path),
            ],
            f"escena {index + 1}",
        )
        segments.append(segment_path)

    concat_file = OUTPUT_DIR / "lista_escenas.txt"
    concat_file.write_text(
        "".join(f"file '{segment.resolve()}'\n" for segment in segments),
        encoding="utf-8",
    )
    silent_video = OUTPUT_DIR / "video_sin_audio.mp4"
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            str(silent_video),
        ],
        "unión de escenas",
    )
    if not silent_video.exists() or silent_video.stat().st_size < 10000:
        raise RuntimeError("FFmpeg no generó el video intermedio.")

    print("Generando narración de voz...")
    voice_file = asyncio.run(generate_voice(data["narration"]))
    final_video = OUTPUT_DIR / "short.mp4"
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-i", str(silent_video),
            "-i", str(voice_file),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
            str(final_video),
        ],
        "mezcla del video y la narración",
    )
    if not final_video.exists() or final_video.stat().st_size < 10000:
        raise RuntimeError("No se pudo crear el archivo final short.mp4.")
    return final_video


def main():
    print("============================================")
    print(" GENERADOR AUTOMATICO DE YOUTUBE SHORTS")
    print(" IA: Groq | Modelo:", GROQ_MODEL)
    print("============================================")

    data, niche = generate_script()
    print("Temática elegida:", niche)
    print("Título:", data["title"])
    print("Palabras de narración:", len(data["narration"].split()))

    images = []
    credits = []
    for index, scene in enumerate(data["scenes"]):
        image_path, credit = find_image(scene["search_term"], index)
        images.append((image_path, scene["caption"]))
        credits.append(credit)

    print("Las cinco imágenes están listas.")
    final_video = create_video(data, images)

    metadata = {
        "provider": "Groq",
        "model": GROQ_MODEL,
        "niche": niche,
        **data,
        "video_file": str(final_video),
        "media_credits": credits,
    }
    (OUTPUT_DIR / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    attribution = [
        "CRÉDITOS DE IMÁGENES - WIKIMEDIA COMMONS",
        "Conservá estos créditos junto al video y respetá los términos de cada licencia.",
        "",
    ]
    for item in credits:
        attribution.append(
            f"Archivo: {item['archivo']}\n"
            f"Autor: {item['autor'] or 'No indicado'}\n"
            f"Licencia: {item['licencia']}\n"
            f"Fuente: {item['fuente']}\n"
        )
    (OUTPUT_DIR / "creditos.txt").write_text(
        "\n".join(attribution), encoding="utf-8"
    )

    (OUTPUT_DIR / "titulo_y_descripcion.txt").write_text(
        f"TÍTULO\n{data['title']}\n\nDESCRIPCIÓN\n{data['description']}\n",
        encoding="utf-8",
    )

    print("============================================")
    print("SHORT GENERADO CORRECTAMENTE")
    print("Archivo:", final_video)
    print("Duración máxima prevista:", VIDEO_SECONDS, "segundos")
    print("============================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("============================================")
        print("ERROR DURANTE LA GENERACIÓN")
        print(type(error).__name__ + ":", str(error))
        print("============================================")
        raise
