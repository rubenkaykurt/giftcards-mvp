import os
import json
import secrets
import sys
from datetime import datetime, timezone

from flask import Flask, request, send_file, abort, jsonify
from dotenv import load_dotenv
from werkzeug.exceptions import HTTPException

import stripe
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail, Attachment, FileContent, FileName, FileType, Disposition
)

import resend

load_dotenv()

app = Flask(__name__)
BASE_DIR = app.root_path

# ====== ENV ======
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "")
BRAND_NAME = os.getenv("BRAND_NAME", "Terapyel")

PDF_DIR = os.getenv("PDF_DIR", "pdfs")
DB_PATH = os.getenv("DB_PATH", "giftcards.json")

# Fondo PNG (opcional)
GIFT_BG_IMAGE = os.getenv("GIFT_BG_IMAGE", "assets/giftcard_bg.png")
GIFT_BG_MODE = os.getenv("GIFT_BG_MODE", "card")  # "card" o "page"

stripe.api_key = STRIPE_SECRET_KEY

SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "")

GIFT_PAYMENT_LINKS = [
    "plink_1T6c9kGX2pDFXvsUAMbtXgXu",
    "plink_1T6c85GX2pDFXvsU1kzTrqAi",
    "plink_1T6c2vGX2pDFXvsUztNJ3wT1",
    "plink_1T6t2uGX2pDFXvsUA9tTz26e",
    "plink_1T7PFNGX2pDFXvsURe9mIM7Z",  # Tarjeta Especial Día de la Mujer
    # Día de la Madre
    "plink_1TPq6xGX2pDFXvsUYRVwHfLd",
    "plink_1TPq9TGX2pDFXvsUNDDw5oe2",
    "plink_1TPqB4GX2pDFXvsUUteTdVjE",
    "plink_1TPrilGX2pDFXvsUqY5UqgHq",
]

# Config por Payment Link (fondo + edición)
GIFT_LINK_CONFIG = {
    # Día del Padre / genéricas anteriores
    "plink_1T6c9kGX2pDFXvsUAMbtXgXu": {
        "edition": "Día del Padre",
        "bg": "assets/giftcard_bg.png",
    },
    "plink_1T6c85GX2pDFXvsU1kzTrqAi": {
        "edition": "Día del Padre",
        "bg": "assets/giftcard_bg.png",
    },
    "plink_1T6c2vGX2pDFXvsUztNJ3wT1": {
        "edition": "Día del Padre",
        "bg": "assets/giftcard_bg.png",
    },
    "plink_1T6t2uGX2pDFXvsUA9tTz26e": {
        "edition": "Día del Padre",
        "bg": "assets/giftcard_bg.png",
    },

    # Día de la Mujer
    "plink_1T7PFNGX2pDFXvsURe9mIM7Z": {
        "edition": "Día de la Mujer",
        "bg": "assets/giftcard_bg_mujer.png",
    },

    # TODO: crear assets/giftcard_bg_madre.png antes de publicar a producción
    # Día de la Madre
    "plink_1TPq6xGX2pDFXvsUYRVwHfLd": {"edition": "Día de la Madre", "bg": "assets/giftcard_bg_madre.png"},
    "plink_1TPq9TGX2pDFXvsUNDDw5oe2": {"edition": "Día de la Madre", "bg": "assets/giftcard_bg_madre.png"},
    "plink_1TPqB4GX2pDFXvsUUteTdVjE": {"edition": "Día de la Madre", "bg": "assets/giftcard_bg_madre.png"},
    "plink_1TPrilGX2pDFXvsUqY5UqgHq": {"edition": "Día de la Madre", "bg": "assets/giftcard_bg_madre.png"},
}


def log(*args):
    print(*args, flush=True)
    sys.stdout.flush()


def abs_asset_path(relative_path: str) -> str:
    if not relative_path:
        return ""
    if os.path.isabs(relative_path):
        return relative_path
    return os.path.join(BASE_DIR, relative_path)


def resolve_bg_path(path_no_fallback: str) -> str:
    """
    Convierte la ruta a absoluta y comprueba si existe.
    Si no existe y termina en .png, prueba también sin extensión.
    """
    if not path_no_fallback:
        return ""

    abs_path = abs_asset_path(path_no_fallback)
    if os.path.exists(abs_path):
        return abs_path

    if abs_path.lower().endswith(".png"):
        alt = abs_path[:-4]
        if os.path.exists(alt):
            return alt

    return abs_path


def abs_runtime_path(path_value: str) -> str:
    if not path_value:
        return ""
    if os.path.isabs(path_value):
        return path_value
    return os.path.join(BASE_DIR, path_value)


# ====== LOGS GLOBALES ======
@app.before_request
def _log_request():
    log("=== INCOMING REQUEST ===")
    log("Remote:", request.remote_addr)
    log("Method:", request.method)
    log("Path:", request.path)
    log("Content-Type:", request.headers.get("Content-Type"))
    log("User-Agent:", request.headers.get("User-Agent"))
    log("Has Stripe-Signature:", bool(request.headers.get("Stripe-Signature")))
    try:
        log("Content-Length:", request.content_length)
    except Exception:
        pass


@app.after_request
def _log_response(resp):
    log("=== RESPONSE ===", resp.status)
    return resp


@app.errorhandler(Exception)
def _log_exception(e):
    log("!!! EXCEPTION !!!", repr(e))
    if isinstance(e, HTTPException):
        return e
    return jsonify({"error": "internal_error"}), 500


# ====== DB ======
def load_db():
    db_path = abs_runtime_path(DB_PATH)

    if not os.path.exists(db_path):
        return {"giftcards": []}

    try:
        with open(db_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                return {"giftcards": []}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"giftcards": []}
            if "giftcards" not in data or not isinstance(data["giftcards"], list):
                return {"giftcards": []}
            return data
    except Exception as e:
        log("DB LOAD FAILED, resetting:", repr(e))
        return {"giftcards": []}


def save_db(db):
    db_path = abs_runtime_path(DB_PATH)
    os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def code_exists(code: str) -> bool:
    db = load_db()
    return any(gc.get("code") == code for gc in db.get("giftcards", []))


def generate_gift_code(amount_eur: int) -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = secrets.token_hex(2).upper()
    return f"TP-{amount_eur}-{date_str}-{suffix}"


def unique_code(amount_eur: int) -> str:
    for _ in range(30):
        code = generate_gift_code(amount_eur)
        if not code_exists(code):
            return code
    raise RuntimeError("No se pudo generar un código único")


def plan_from_amount(amount_eur: int, edition_label: str = "Día del Padre", payment_link_id: str = "") -> dict:
    # Caso especial: tarjeta Día de la Mujer
    if payment_link_id == "plink_1T7PFNGX2pDFXvsURe9mIM7Z":
        return {
            "plan": "Tarjeta Regalo",
            "promo_value": (
                "Tarjeta especial Día de la Mujer. "
                "Valor real 285€ en tratamientos, adquirida por 200€ durante la promoción. "
                "Validez: 12 meses. Utilizable en todos los tratamientos de Terapyel."
            ),
            "note": ""
        }

    if edition_label == "Día de la Madre":
        madre_promo = (
            "Saldo canjeable por cualquier tratamiento de Terapyel. "
            "Incluye un 15 % de descuento extra al canjearla. Validez 12 meses."
        )
        if amount_eur == 68:
            plan = "Pausa"
        elif amount_eur == 170:
            plan = "Renacer"
        elif amount_eur == 299:
            plan = "Mimos"
        else:
            plan = "Tarjeta Regalo"
        return {"plan": plan, "promo_value": madre_promo, "note": ""}

    if amount_eur == 68:
        return {
            "plan": "Essential",
            "promo_value": "Cubre una Limpieza Facial Peel & Glow (valorada en 80€)",
            "note": f"Ejemplo de canje durante la promoción {edition_label}."
        }
    if amount_eur == 170:
        return {
            "plan": "Signature",
            "promo_value": "Puede cubrir, por ejemplo, un PRP capilar (valorado en 200€)",
            "note": f"Ejemplo de canje durante la promoción {edition_label}."
        }
    if amount_eur == 299:
        return {
            "plan": "Prestige",
            "promo_value": "Puede cubrir, por ejemplo, un tratamiento de bótox (según valoración médica)",
            "note": f"Ejemplo de canje durante la promoción {edition_label}."
        }

    return {
        "plan": "Tarjeta Regalo",
        "promo_value": "Importe canjeable por tratamientos/servicios hasta el valor de la tarjeta.",
        "note": "Canje sujeto a valoración y disponibilidad."
    }


def euros_from_stripe_amount(amount_total: int, currency: str) -> int:
    return int(round(amount_total / 100))


# ====== PDF HELPERS ======
def wrap_text_width(text: str, font_name: str, font_size: int, max_width: float):
    words = (text or "").split()
    lines = []
    current = ""

    for w in words:
        candidate = (current + " " + w).strip()
        if stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = w

    if current:
        lines.append(current)
    return lines


def generate_pdf(
    filepath: str,
    code: str,
    amount_eur: int,
    buyer_email: str,
    edition_label: str,
    bg_image_path: str,
    payment_link_id: str
):
    meta = plan_from_amount(
        amount_eur,
        edition_label=edition_label,
        payment_link_id=payment_link_id
    )
    plan = meta["plan"]
    promo_value = meta["promo_value"]
    note = meta["note"]

    c = canvas.Canvas(filepath, pagesize=A4)
    w, h = A4

    # ====== Layout base ======
    margin = 18 * mm
    card_x, card_y = margin, margin
    card_w, card_h = w - 2 * margin, h - 2 * margin

    inner_pad_x = 18 * mm
    left_x = card_x + inner_pad_x
    right_x = card_x + card_w - inner_pad_x
    max_text_width = right_x - left_x

    # ====== CAPA 1: Fondo PNG ======
    bg_drawn = False
    log("generate_pdf() bg_image_path recibido:", bg_image_path)

    bg_path = resolve_bg_path(bg_image_path or GIFT_BG_IMAGE)
    log("generate_pdf() bg_path resuelto:", bg_path)
    log("generate_pdf() bg exists:", os.path.exists(bg_path))

    if bg_path and os.path.exists(bg_path):
        log("Usando fondo PNG:", bg_path)
        try:
            bg = ImageReader(bg_path)
            c.drawImage(
                bg,
                0, 0,
                width=w,
                height=h,
                preserveAspectRatio=False,
                mask="auto"
            )
            bg_drawn = True
        except Exception as e:
            log("Error loading background:", repr(e))

    if not bg_drawn:
        log("FALLBACK: no se pudo usar fondo PNG, se dibuja fondo por defecto")
        c.setFillColorRGB(0.03, 0.04, 0.07)
        c.rect(0, 0, w, h, fill=1, stroke=0)
        c.setFillColorRGB(0.05, 0.07, 0.12)
        c.roundRect(card_x, card_y, card_w, card_h, 18, fill=1, stroke=0)

    # ====== CAPA 2: Texto ======
    title_y = card_y + card_h - 28 * mm
    c.setFillColorRGB(0.92, 0.94, 1.0)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(left_x, title_y, f"{BRAND_NAME} · Tarjeta Regalo")

    subtitle_y = title_y - 10 * mm
    c.setFont("Helvetica", 13)
    c.setFillColorRGB(0.78, 0.82, 0.92)
    c.drawString(left_x, subtitle_y, f"Edición {edition_label}")

    plan_y = subtitle_y - 20 * mm
    c.setFillColorRGB(0.95, 0.83, 0.54)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(left_x, plan_y, f"{plan}")

    c.setFont("Helvetica-Bold", 34)
    c.drawRightString(right_x, plan_y, f"{amount_eur}€")

    code_title_y = plan_y - 22 * mm
    c.setFillColorRGB(0.92, 0.94, 1.0)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(left_x, code_title_y, "Código")

    code_value_y = code_title_y - 10 * mm
    c.setFont("Helvetica", 14)
    c.drawString(left_x, code_value_y, code)

    benefit_title_y = code_value_y - 26 * mm
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(0.92, 0.94, 1.0)
    c.drawString(left_x, benefit_title_y, "Beneficio promoción")

    benefit_text = promo_value if not note else f"{promo_value}. {note}"
    benefit_font = "Helvetica"
    benefit_size = 12
    leading = 16

    c.setFont(benefit_font, benefit_size)
    c.setFillColorRGB(0.78, 0.82, 0.92)

    benefit_lines = wrap_text_width(
        benefit_text,
        font_name=benefit_font,
        font_size=benefit_size,
        max_width=max_text_width
    )

    benefit_start_y = benefit_title_y - 6 * mm
    text_obj = c.beginText(left_x, benefit_start_y)
    text_obj.setLeading(leading)
    for line in benefit_lines:
        text_obj.textLine(line)
    c.drawText(text_obj)

    benefit_end_y = benefit_start_y - leading * len(benefit_lines)

    # Zona reservada para imagen/diseño
    photo_top_y = card_y + 95 * mm
    photo_bottom_y = card_y + 60 * mm

    # Cómo canjear
    canjear_title_y = min(benefit_end_y - 10 * mm, photo_top_y - 10 * mm)
    canjear_title_y = max(canjear_title_y, card_y + 110 * mm)

    c.setFillColorRGB(0.92, 0.94, 1.0)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(left_x, canjear_title_y, "Cómo canjear")

    c.setFillColorRGB(0.78, 0.82, 0.92)
    c.setFont("Helvetica", 12)

    canje = [
        "1) Reserva tu cita.",
        "2) Indica este código al equipo.",
        "3) Se descontará del total hasta el importe de la tarjeta."
    ]
    y = canjear_title_y - 10 * mm
    for line in canje:
        c.drawString(left_x, y, line)
        y -= 8 * mm

    footer_y = card_y + 18 * mm
    c.setFillColorRGB(0.62, 0.66, 0.78)
    c.setFont("Helvetica", 10)
    c.drawString(left_x, footer_y, f"Comprador: {buyer_email}")
    c.drawRightString(
        right_x,
        footer_y - 10,
        "No canjeable por dinero. Sujeto a disponibilidad y valoración."
    )

    c.showPage()
    c.save()


# ====== EMAIL (SendGrid) ======
def send_email_with_pdf(to_email: str, subject: str, body: str, pdf_path: str):
    if not SENDGRID_API_KEY or not FROM_EMAIL:
        raise RuntimeError("Faltan SENDGRID_API_KEY o FROM_EMAIL en variables de entorno")

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    import base64
    encoded = base64.b64encode(pdf_bytes).decode()

    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=to_email,
        subject=subject,
        html_content=body
    )

    attachment = Attachment(
        FileContent(encoded),
        FileName(os.path.basename(pdf_path)),
        FileType("application/pdf"),
        Disposition("attachment")
    )
    message.attachment = attachment

    sg = SendGridAPIClient(SENDGRID_API_KEY)
    sg.send(message)


def send_email_with_pdf_resend(to_email: str, subject: str, body: str, pdf_path: str):
    print(f"=== RESEND SEND START === to={to_email}", flush=True)
    resend.api_key = os.environ["RESEND_API_KEY"]
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    params = {
        "from": os.environ.get("EMAIL_FROM", "Terapyel <info@terapyel.com>"),
        "to": [to_email],
        "reply_to": "info@terapyel.com",
        "subject": subject,
        "html": body,
        "attachments": [{
            "filename": os.path.basename(pdf_path),
            "content": list(pdf_bytes),
        }],
    }
    result = resend.Emails.send(params)
    print(f"=== RESEND SEND OK === id={result.get('id')}", flush=True)
    return result


def push_to_google_sheets(fecha_iso: str, codigo: str, cliente: str, importe: int):
    if not SHEETS_WEBHOOK_URL:
        log("SHEETS_WEBHOOK_URL vacío: no se envía a Google Sheets")
        return

    payload = {
        "fecha": fecha_iso,
        "codigo": codigo,
        "cliente": cliente,
        "importe": importe
    }

    try:
        import requests
        r = requests.post(SHEETS_WEBHOOK_URL, json=payload, timeout=10)
        log("Sheets push status:", r.status_code, "resp:", r.text[:200])
    except Exception as e:
        log("Sheets push FAILED:", repr(e))


# ====== WEBHOOK ======
@app.route("/stripe/webhook", methods=["POST"], strict_slashes=False)
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    log("=== STRIPE WEBHOOK HIT ===")
    log("Payload bytes:", len(payload) if payload else 0)
    log("Stripe-Signature present:", bool(sig_header))
    log("Webhook secret present:", bool(STRIPE_WEBHOOK_SECRET))
    log("Stripe key starts with:", (STRIPE_SECRET_KEY[:7] + "...") if STRIPE_SECRET_KEY else "EMPTY")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET
        )
        log("Event type:", event.get("type"))
        log("Livemode:", event.get("livemode"))
    except Exception as e:
        log("Webhook construct_event FAILED:", repr(e))
        return jsonify({"error": f"Webhook error: {str(e)}"}), 400

    if event.get("type") != "checkout.session.completed":
        return jsonify({"received": True, "ignored": event.get("type")}), 200

    session = event["data"]["object"]
    payment_link_id = session.get("payment_link")

    cfg = GIFT_LINK_CONFIG.get(
        payment_link_id,
        {"edition": "Día del Padre", "bg": GIFT_BG_IMAGE}
    )
    edition_label = cfg.get("edition", "Día del Padre")
    bg_image_path = cfg.get("bg", GIFT_BG_IMAGE)

    log("Payment link recibido:", payment_link_id)
    log("Edition elegida:", edition_label)
    log("BG configurado:", bg_image_path)
    log("BG resuelto:", resolve_bg_path(bg_image_path))
    log("BG existe:", os.path.exists(resolve_bg_path(bg_image_path)))

    if payment_link_id not in GIFT_PAYMENT_LINKS:
        log("Ignoring payment from non-giftcard payment link:", payment_link_id)
        return jsonify({"received": True, "ignored": "not_giftcard"}), 200

    buyer_email = (session.get("customer_details") or {}).get("email") or session.get("customer_email")
    if not buyer_email:
        return jsonify({"error": "No buyer email in session"}), 400

    amount_total = session.get("amount_total")
    currency = (session.get("currency") or "eur").upper()
    if amount_total is None:
        return jsonify({"error": "No amount_total in session"}), 400

    amount_eur = euros_from_stripe_amount(amount_total, currency)

    pdf_dir_abs = abs_runtime_path(PDF_DIR)
    if not os.path.isdir(pdf_dir_abs):
        os.makedirs(pdf_dir_abs, exist_ok=True)

    session_id = session.get("id")
    db = load_db()
    if any(gc.get("stripe_session_id") == session_id for gc in db.get("giftcards", [])):
        return jsonify({"ok": True, "deduped": True}), 200

    code = unique_code(amount_eur)
    pdf_filename = f"giftcard_{code}.pdf"
    pdf_path = os.path.join(pdf_dir_abs, pdf_filename)

    generate_pdf(
        pdf_path,
        code=code,
        amount_eur=amount_eur,
        buyer_email=buyer_email,
        edition_label=edition_label,
        bg_image_path=bg_image_path,
        payment_link_id=payment_link_id
    )

    created_at = datetime.now(timezone.utc).isoformat()
    record = {
        "code": code,
        "amount_eur": amount_eur,
        "currency": currency,
        "buyer_email": buyer_email,
        "created_at": created_at,
        "pdf_path": pdf_path,
        "stripe_session_id": session_id,
        "status": "issued"
    }
    db["giftcards"].append(record)
    save_db(db)

    push_to_google_sheets(
        fecha_iso=created_at,
        codigo=code,
        cliente=buyer_email,
        importe=amount_eur
    )

    subject = f"Tu Tarjeta Regalo {BRAND_NAME} · {edition_label} ({amount_eur}€)"
    if edition_label == "Día de la Madre":
        email_h2 = "¡Tu tarjeta para el Día de la Madre está lista! 🌸"
    elif edition_label == "Día de la Mujer":
        email_h2 = "¡Tu tarjeta para el Día de la Mujer está lista!"
    else:
        email_h2 = "¡Gracias por tu compra!"
    body = f"""
    <div style="font-family:Arial,Helvetica,sans-serif; line-height:1.5">
      <h2 style="margin:0 0 8px">{email_h2}</h2>
      <p style="margin:0 0 12px">Adjuntamos tu tarjeta regalo en PDF.</p>
      <p style="margin:0 0 12px">
        <strong>Código:</strong> {code}<br/>
        <strong>Importe:</strong> {amount_eur}€
      </p>
      <p style="margin:0 0 12px; color:#555">
        Si necesitas que la reenviemos o tienes dudas, responde a este correo o escríbenos por WhatsApp.
      </p>
    </div>
    """

    try:
        send_email_with_pdf_resend(
            to_email=buyer_email,
            subject=subject,
            body=body,
            pdf_path=pdf_path
        )
    except Exception as e:
        log(f"=== RESEND SEND FAILED === error={repr(e)}")
        record["status"] = "issued_email_failed"
        record["email_error"] = str(e)
        save_db(db)
        return jsonify({"ok": True, "email_sent": False, "error": str(e), "code": code}), 200

    return jsonify({"ok": True, "email_sent": True, "code": code}), 200


# Descarga por código
@app.get("/giftcards/<code>")
def download_giftcard(code: str):
    db = load_db()
    gc = next((x for x in db.get("giftcards", []) if x.get("code") == code), None)
    if not gc:
        abort(404)

    pdf_path = gc.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        abort(404)

    return send_file(pdf_path, as_attachment=True, download_name=os.path.basename(pdf_path))


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({"ok": True}), 200


@app.get("/")
def root():
    return jsonify({"ok": True, "service": "giftcards-mvp"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))