import os
import json
import hmac
import hashlib
import secrets
import sys
from datetime import datetime, timezone

from flask import Flask, request, send_file, abort, jsonify
from dotenv import load_dotenv

import stripe
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

load_dotenv()

app = Flask(__name__)

# ====== ENV ======
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "")
BRAND_NAME = os.getenv("BRAND_NAME", "Terapyel")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # ej: https://tuapp.onrender.com
PDF_DIR = os.getenv("PDF_DIR", "pdfs")
DB_PATH = os.getenv("DB_PATH", "giftcards.json")

# Validación mínima
if not os.path.isdir(PDF_DIR):
    os.makedirs(PDF_DIR, exist_ok=True)

stripe.api_key = STRIPE_SECRET_KEY


# ====== Utilidades ======
def load_db():
    if not os.path.exists(DB_PATH):
        return {"giftcards": []}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def code_exists(code: str) -> bool:
    db = load_db()
    return any(gc.get("code") == code for gc in db.get("giftcards", []))

def generate_gift_code(amount_eur: int) -> str:
    # Ejemplo: TP-170-20260302-K4D9
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    # 4 chars seguros
    suffix = secrets.token_hex(2).upper()  # 4 hex chars
    prefix = "TP"
    return f"{prefix}-{amount_eur}-{date_str}-{suffix}"

def unique_code(amount_eur: int) -> str:
    for _ in range(30):
        code = generate_gift_code(amount_eur)
        if not code_exists(code):
            return code
    # muy improbable
    raise RuntimeError("No se pudo generar un código único")

def plan_from_amount(amount_eur: int) -> dict:
    """
    Ajustado a tu promo:
    Essential: 68€ -> ejemplo Peel & Glow (80€)
    Signature: 170€ -> ejemplo PRP capilar (200€)
    Prestige: 299€ -> ejemplo bótox (según valoración)
    """
    if amount_eur == 68:
        return {
            "plan": "Essential",
            "promo_value": "Cubre una Limpieza Facial Peel & Glow (valorada en 80€)",
            "note": "Ejemplo de canje durante la promoción Día del Padre."
        }
    if amount_eur == 170:
        return {
            "plan": "Signature",
            "promo_value": "Puede cubrir, por ejemplo, un PRP capilar (valorado en 200€)",
            "note": "Ejemplo de canje durante la promoción Día del Padre."
        }
    if amount_eur == 299:
        return {
            "plan": "Prestige",
            "promo_value": "Puede cubrir, por ejemplo, un tratamiento de bótox (según valoración médica)",
            "note": "Ejemplo de canje durante la promoción Día del Padre."
        }
    return {
        "plan": "Tarjeta Regalo",
        "promo_value": "Importe canjeable por tratamientos/servicios hasta el valor de la tarjeta.",
        "note": "Canje sujeto a valoración y disponibilidad."
    }

def euros_from_stripe_amount(amount_total: int, currency: str) -> int:
    # Stripe envía amount_total en céntimos para EUR
    if currency.upper() == "EUR":
        return int(round(amount_total / 100))
    # si usas otra moneda, ajusta
    return int(round(amount_total / 100))

def log(*args):
    print(*args, flush=True)
    sys.stdout.flush()


# ====== PDF ======
def generate_pdf(filepath: str, code: str, amount_eur: int, buyer_email: str):
    meta = plan_from_amount(amount_eur)
    plan = meta["plan"]
    promo_value = meta["promo_value"]
    note = meta["note"]

    c = canvas.Canvas(filepath, pagesize=A4)
    w, h = A4

    # Fondo premium simple (sin imágenes)
    c.setFillColorRGB(0.03, 0.04, 0.07)
    c.rect(0, 0, w, h, fill=1, stroke=0)

    # “Tarjeta” interior
    margin = 18 * mm
    card_x, card_y = margin, margin
    card_w, card_h = w - 2 * margin, h - 2 * margin

    c.setFillColorRGB(0.05, 0.07, 0.12)
    c.roundRect(card_x, card_y, card_w, card_h, 18, fill=1, stroke=0)

    # Título
    c.setFillColorRGB(0.92, 0.94, 1.0)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(card_x + 18*mm, card_y + card_h - 28*mm, f"{BRAND_NAME} · Tarjeta Regalo")

    c.setFont("Helvetica", 13)
    c.setFillColorRGB(0.78, 0.82, 0.92)
    c.drawString(card_x + 18*mm, card_y + card_h - 38*mm, "Edición Día del Padre")

    # Plan + Importe
    c.setFillColorRGB(0.95, 0.83, 0.54)  # dorado
    c.setFont("Helvetica-Bold", 18)
    c.drawString(card_x + 18*mm, card_y + card_h - 58*mm, f"{plan}")

    c.setFont("Helvetica-Bold", 34)
    c.drawRightString(card_x + card_w - 18*mm, card_y + card_h - 58*mm, f"{amount_eur}€")

    # Código
    c.setFillColorRGB(0.92, 0.94, 1.0)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(card_x + 18*mm, card_y + card_h - 78*mm, "Código")
    c.setFont("Helvetica", 14)
    c.drawString(card_x + 18*mm, card_y + card_h - 88*mm, code)

    # Beneficio promo (texto)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(card_x + 18*mm, card_y + card_h - 110*mm, "Beneficio promoción")
    c.setFont("Helvetica", 12)
    c.setFillColorRGB(0.78, 0.82, 0.92)

    text = c.beginText(card_x + 18*mm, card_y + card_h - 122*mm)
    text.setLeading(16)
    for line in wrap_text(f"{promo_value}. {note}", 85):
        text.textLine(line)
    c.drawText(text)

    # Instrucciones
    c.setFillColorRGB(0.92, 0.94, 1.0)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(card_x + 18*mm, card_y + 62*mm, "Cómo canjear")
    c.setFillColorRGB(0.78, 0.82, 0.92)
    c.setFont("Helvetica", 12)

    canje = [
        "1) Reserva tu cita.",
        "2) Indica este código al equipo.",
        "3) Se descontará del total hasta el importe de la tarjeta."
    ]
    y = card_y + 54*mm
    for line in canje:
        c.drawString(card_x + 18*mm, y, line)
        y -= 8*mm

    # Pie
    c.setFillColorRGB(0.62, 0.66, 0.78)
    c.setFont("Helvetica", 10)
    c.drawString(card_x + 18*mm, card_y + 18*mm, f"Comprador: {buyer_email}")
    c.drawRightString(card_x + card_w - 18*mm, card_y + 18*mm, "No canjeable por dinero. Sujeto a disponibilidad y valoración.")

    c.showPage()
    c.save()

def wrap_text(s: str, max_chars: int):
    words = s.split()
    lines, current = [], []
    count = 0
    for w in words:
        if count + len(w) + (1 if current else 0) > max_chars:
            lines.append(" ".join(current))
            current = [w]
            count = len(w)
        else:
            current.append(w)
            count += len(w) + (1 if len(current) > 1 else 0)
    if current:
        lines.append(" ".join(current))
    return lines


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


# ====== WEBHOOK ======
@app.route("/stripe/webhook", methods=["POST"], strict_slashes=False)
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")
log("=== STRIPE WEBHOOK HIT ===")
log("Method:", request.method)
log("Path:", request.path)
log("Content-Type:", request.headers.get("Content-Type"))
log("Stripe-Signature present:", bool(sig_header))
log("Payload bytes:", len(payload) if payload else 0)
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET
        )

        log("Event type:", event.get("type"))
log("Livemode:", event.get("livemode"))
    except Exception as e:
        # Firma inválida o payload corrupto
        return jsonify({"error": f"Webhook error: {str(e)}"}), 400

    # Solo procesamos el evento clave
    if event["type"] != "checkout.session.completed":
        return jsonify({"received": True, "ignored": event["type"]}), 200

    session = event["data"]["object"]

    # Datos del comprador
    buyer_email = (
        session.get("customer_details", {}) or {}
    ).get("email") or session.get("customer_email") or None

    if not buyer_email:
        # Sin email, no podemos enviar la tarjeta
        return jsonify({"error": "No buyer email in session"}), 400

    amount_total = session.get("amount_total")
    currency = (session.get("currency") or "eur").upper()
    if amount_total is None:
        return jsonify({"error": "No amount_total in session"}), 400

    amount_eur = euros_from_stripe_amount(amount_total, currency)

    # Idempotencia: evita duplicados si Stripe reintenta
    session_id = session.get("id")
    db = load_db()
    if any(gc.get("stripe_session_id") == session_id for gc in db.get("giftcards", [])):
        return jsonify({"ok": True, "deduped": True}), 200

    # Generar código y PDF
    code = unique_code(amount_eur)
    pdf_filename = f"giftcard_{code}.pdf"
    pdf_path = os.path.join(PDF_DIR, pdf_filename)
    generate_pdf(pdf_path, code=code, amount_eur=amount_eur, buyer_email=buyer_email)

    # Guardar registro
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

    # Email
    subject = f"Tu Tarjeta Regalo {BRAND_NAME} · Día del Padre ({amount_eur}€)"
    body = f"""
    <div style="font-family:Arial,Helvetica,sans-serif; line-height:1.5">
      <h2 style="margin:0 0 8px">¡Gracias por tu compra!</h2>
      <p style="margin:0 0 12px">
        Adjuntamos tu tarjeta regalo en PDF.
      </p>
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
        send_email_with_pdf(to_email=buyer_email, subject=subject, body=body, pdf_path=pdf_path)
    except Exception as e:
        # Marcamos como emitida pero sin email (para reintentar manualmente)
        record["status"] = "issued_email_failed"
        record["email_error"] = str(e)
        save_db(db)
        return jsonify({"ok": True, "email_sent": False, "error": str(e), "code": code}), 200

    return jsonify({"ok": True, "email_sent": True, "code": code}), 200


# (Opcional) endpoint de descarga por código
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


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    # Para local
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
