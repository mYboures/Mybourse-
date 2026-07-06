#!/usr/bin/env python3
"""
Scholarship Alert - Genie Civil
--------------------------------
Scanne des flux RSS (Google News + portails de bourses connus) a la recherche
de nouvelles bourses en genie civil, et envoie un email recapitulatif des
nouveautes.

Usage:
    python scholarship_alert.py

Configuration:
    Remplis la section CONFIG ci-dessous (mots-cles, email, etc.)
    puis programme ce script via cron / Planificateur de taches pour un run quotidien.
"""

import os

try:
    from dotenv import load_dotenv  # pip install python-dotenv
    load_dotenv()  # charge automatiquement le fichier .env s'il existe
except ImportError:
    pass  # pas grave si absent : le script utilisera alors les valeurs par defaut

import re
import sqlite3
import smtplib
import ssl
import time
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import feedparser  # pip install feedparser
import requests     # pip install requests

# =========================== CONFIG ===========================

# Mots-cles de recherche (FR + EN pour couvrir plus de sources)
KEYWORDS = [
    "bourse génie civil",
    "bourse d'étude génie civil",
    "civil engineering scholarship",
    "scholarship civil engineering master",
    "scholarship civil engineering PhD",
    "bourse ingénieur BTP",
]

# Flux RSS de portails de bourses connus (tu peux en ajouter d'autres)
STATIC_RSS_FEEDS = [
    "https://www.campusfrance.org/fr/rss/bourses",           # Campus France (a verifier/adapter si l'URL change)
    "https://www.scholars4dev.com/feed/",                     # Scholars4Dev (portail generaliste, filtre par mot-cle)
    "https://opportunitiescorners.com/feed/",                 # Opportunities Corners
]

# Parametres email (utiliser un "mot de passe d'application" pour Gmail/Outlook, pas ton mot de passe principal)
# En local : modifie directement les valeurs par defaut ci-dessous.
# Sur GitHub Actions : ces valeurs viennent des "Secrets" du depot (voir README).
EMAIL_CONFIG = {
    "smtp_server": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
    "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
    "sender_email": os.environ.get("SENDER_EMAIL", "TON_EMAIL@gmail.com"),
    "sender_password": os.environ.get("SENDER_PASSWORD", "TON_MOT_DE_PASSE_APPLICATION"),
    "recipient_email": os.environ.get("RECIPIENT_EMAIL", "TON_EMAIL_DESTINATAIRE@gmail.com"),
}

# Telegram : cree un bot via @BotFather sur Telegram pour obtenir le token,
# puis envoie-lui un message et va sur https://api.telegram.org/bot<TOKEN>/getUpdates
# pour recuperer ton chat_id.
TELEGRAM_CONFIG = {
    "enabled": True,
    "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "TON_TOKEN_TELEGRAM"),
    "chat_id": os.environ.get("TELEGRAM_CHAT_ID", "TON_CHAT_ID"),
}

# WhatsApp via CallMeBot (gratuit, simple, pour usage personnel) :
# 1. Ajoute ce contact dans WhatsApp : +34 644 84 71 65
# 2. Envoie-lui le message : "I allow callmebot to send me messages"
# 3. Il te repond avec ton "apikey" a mettre ci-dessous
WHATSAPP_CONFIG = {
    "enabled": False,  # remets a True quand tu auras configure ta cle CallMeBot
    "phone_number": os.environ.get("WHATSAPP_PHONE", "TON_NUMERO_AVEC_INDICATIF"),  # ex: "225XXXXXXXXX" (sans le +)
    "apikey": os.environ.get("WHATSAPP_APIKEY", "TON_APIKEY_CALLMEBOT"),
}

DB_PATH = "seen_scholarships.db"
# Le dashboard est place dans docs/ pour pouvoir etre publie directement via GitHub Pages
DASHBOARD_PATH = os.path.join("docs", "index.html")

# =========================== BASE DE DONNEES (dedup) ===========================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            link TEXT PRIMARY KEY,
            title TEXT,
            date_found TEXT,
            source TEXT,
            image TEXT,
            summary TEXT,
            published TEXT
        )
    """)
    # Migration douce si la base existait deja avec l'ancien schema (sans ces colonnes)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(seen)")}
    for col in ["source", "image", "summary", "published"]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE seen ADD COLUMN {col} TEXT")
    conn.commit()
    return conn


def is_new(conn, link):
    cur = conn.execute("SELECT 1 FROM seen WHERE link = ?", (link,))
    return cur.fetchone() is None


def mark_seen(conn, item):
    conn.execute(
        """INSERT OR IGNORE INTO seen (link, title, date_found, source, image, summary, published)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            item["link"],
            item["title"],
            datetime.now().isoformat(),
            item.get("source", ""),
            item.get("image") or "",
            item.get("summary", ""),
            item.get("published", ""),
        ),
    )
    conn.commit()


# =========================== COLLECTE ===========================

def google_news_rss_url(keyword, lang="fr", country="FR"):
    q = urllib.parse.quote(keyword)
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={country}&ceid={country}:{lang}"


def fetch_feed(url):
    try:
        feed = feedparser.parse(url)
        return feed.entries
    except Exception as e:
        print(f"[!] Erreur lecture flux {url} : {e}")
        return []


def extract_image(entry):
    """Essaie de recuperer une image/flyer associee a l'annonce, si le flux RSS en fournit une."""
    # Cas 1 : media_thumbnail (souvent utilise par Google News et les flux WordPress)
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url")
    # Cas 2 : media_content
    if hasattr(entry, "media_content") and entry.media_content:
        return entry.media_content[0].get("url")
    # Cas 3 : enclosure (piece jointe image dans le flux)
    for link_info in entry.get("links", []):
        if link_info.get("type", "").startswith("image"):
            return link_info.get("href")
    return None


def clean_summary(raw_html):
    """Retire les balises HTML d'un resume RSS pour en faire du texte lisible."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]  # on garde un extrait raisonnable


def collect_new_scholarships(conn):
    new_items = []

    # 1. Recherche par mots-cles via Google News RSS
    for kw in KEYWORDS:
        url = google_news_rss_url(kw)
        entries = fetch_feed(url)
        print(f"[i] Mot-cle '{kw}' -> {len(entries)} resultat(s) trouve(s)")
        for entry in entries:
            link = entry.get("link", "")
            title = entry.get("title", "")
            if link and is_new(conn, link):
                item = {
                    "title": title,
                    "link": link,
                    "source": f"Google News ({kw})",
                    "published": entry.get("published", ""),
                    "image": extract_image(entry),
                    "summary": clean_summary(entry.get("summary", "")),
                }
                new_items.append(item)
                mark_seen(conn, item)

    # 2. Flux RSS statiques (portails de bourses)
    for url in STATIC_RSS_FEEDS:
        entries = fetch_feed(url)
        print(f"[i] Flux '{url}' -> {len(entries)} resultat(s) trouve(s)")
        for entry in entries:
            link = entry.get("link", "")
            title = entry.get("title", "")
            if not link or not is_new(conn, link):
                continue
            # Filtre grossier : on ne garde que si le titre/resume evoque le genie civil
            text = (title + " " + entry.get("summary", "")).lower()
            if any(k in text for k in ["civil", "génie civil", "genie civil", "btp", "construction engineering"]):
                item = {
                    "title": title,
                    "link": link,
                    "source": url,
                    "published": entry.get("published", ""),
                    "image": extract_image(entry),
                    "summary": clean_summary(entry.get("summary", "")),
                }
                new_items.append(item)
                mark_seen(conn, item)

    return new_items


# =========================== NOTIFICATION EMAIL ===========================

def send_email(new_items):
    if not new_items:
        print("Aucune nouvelle bourse trouvee aujourd'hui.")
        return

    subject = f"🎓 {len(new_items)} nouvelle(s) bourse(s) en génie civil - {datetime.now().strftime('%d/%m/%Y')}"

    # Version texte simple (pour les clients mail qui n'affichent pas le HTML)
    body_lines = [f"Voici {len(new_items)} nouvelle(s) opportunité(s) trouvée(s) aujourd'hui :\n"]
    for item in new_items:
        body_lines.append(f"- {item['title']}\n  Lien : {item['link']}\n  Source : {item['source']}\n")
    text_body = "\n".join(body_lines)

    # Version HTML (affiche le flyer/l'image de l'annonce quand elle est disponible)
    html_blocks = [f"<p>Voici {len(new_items)} nouvelle(s) opportunité(s) trouvée(s) aujourd'hui :</p>"]
    for item in new_items:
        image_html = (
            f'<img src="{item["image"]}" alt="flyer" style="max-width:400px;display:block;margin:8px 0;">'
            if item.get("image") else ""
        )
        html_blocks.append(
            f'<div style="margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid #ddd;">'
            f'<strong>{item["title"]}</strong><br>'
            f'{image_html}'
            f'<a href="{item["link"]}">Voir l\'annonce</a> — Source : {item["source"]}'
            f'</div>'
        )
    html_body = "".join(html_blocks)

    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_CONFIG["sender_email"]
    msg["To"] = EMAIL_CONFIG["recipient_email"]
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"], context=context) as server:
            server.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])
            server.send_message(msg)
        print(f"[✓] Email envoyé avec {len(new_items)} nouvelle(s) bourse(s).")
    except Exception as e:
        print(f"[!] Erreur lors de l'envoi de l'email : {e}")


# =========================== NOTIFICATION TELEGRAM ===========================

def send_telegram(new_items):
    if not TELEGRAM_CONFIG.get("enabled"):
        return
    if not new_items:
        return

    base_url = f"https://api.telegram.org/bot{TELEGRAM_CONFIG['bot_token']}"

    # Annonces avec flyer/image : envoyees en tant que photo avec legende
    with_image = [item for item in new_items if item.get("image")]
    without_image = [item for item in new_items if not item.get("image")]

    for item in with_image:
        caption = f"🎓 {item['title']}\n{item['link']}"[:1024]  # limite Telegram pour les legendes
        try:
            resp = requests.post(f"{base_url}/sendPhoto", data={
                "chat_id": TELEGRAM_CONFIG["chat_id"],
                "photo": item["image"],
                "caption": caption,
            }, timeout=15)
            if resp.status_code == 200:
                print(f"[✓] Photo Telegram envoyée : {item['title'][:50]}")
            else:
                # Si l'image ne peut pas etre chargee par Telegram, on bascule sur un message texte
                print(f"[!] Erreur photo Telegram ({resp.status_code}), envoi en texte a la place")
                without_image.append(item)
        except Exception as e:
            print(f"[!] Erreur lors de l'envoi de la photo Telegram : {e}")
            without_image.append(item)
        time.sleep(1)  # eviter le rate-limit Telegram

    # Le reste (sans image) regroupe dans un seul message recapitulatif
    if without_image:
        header = f"🎓 {len(without_image)} autre(s) bourse(s) en génie civil ({datetime.now().strftime('%d/%m/%Y')})\n\n"
        body_lines = [header]
        for item in without_image:
            body_lines.append(f"• {item['title']}\n{item['link']}\n")
        full_text = "\n".join(body_lines)

        # Telegram limite les messages a 4096 caracteres : on decoupe si besoin
        chunks = [full_text[i:i + 4000] for i in range(0, len(full_text), 4000)]

        for chunk in chunks:
            try:
                resp = requests.post(f"{base_url}/sendMessage", data={
                    "chat_id": TELEGRAM_CONFIG["chat_id"],
                    "text": chunk,
                    "disable_web_page_preview": True,
                }, timeout=15)
                if resp.status_code == 200:
                    print("[✓] Message Telegram envoyé.")
                else:
                    print(f"[!] Erreur Telegram : {resp.status_code} {resp.text}")
            except Exception as e:
                print(f"[!] Erreur lors de l'envoi Telegram : {e}")
            time.sleep(1)  # eviter le rate-limit Telegram


# =========================== NOTIFICATION WHATSAPP (CallMeBot) ===========================

def send_whatsapp(new_items):
    if not WHATSAPP_CONFIG.get("enabled"):
        return
    if not new_items:
        return

    url = "https://api.callmebot.com/whatsapp.php"

    # CallMeBot fonctionne mieux avec des messages courts : on envoie un resume,
    # puis un message par bourse si la liste est courte (sinon juste le resume + lien du 1er).
    header = f"🎓 {len(new_items)} nouvelle(s) bourse(s) genie civil ({datetime.now().strftime('%d/%m/%Y')}):\n"
    lines = [header]
    for item in new_items[:10]:  # limite pour rester lisible sur WhatsApp
        line = f"- {item['title']} : {item['link']}"
        if item.get("image"):
            line += f"\n  📎 Flyer : {item['image']}"
        lines.append(line)
    if len(new_items) > 10:
        lines.append(f"... et {len(new_items) - 10} autre(s).")
    text = "\n".join(lines)

    try:
        resp = requests.get(url, params={
            "phone": WHATSAPP_CONFIG["phone_number"],
            "text": text,
            "apikey": WHATSAPP_CONFIG["apikey"],
        }, timeout=15)
        if resp.status_code == 200:
            print("[✓] Message WhatsApp envoyé.")
        else:
            print(f"[!] Erreur WhatsApp : {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[!] Erreur lors de l'envoi WhatsApp : {e}")


# =========================== SITE WEB (DASHBOARD LOCAL) ===========================

def html_escape(text):
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def generate_dashboard(conn):
    """Genere un site web local (fichier HTML) listant toutes les bourses trouvees,
    avec une fenetre (modale) par annonce montrant le resume et le lien pour postuler."""
    rows = conn.execute(
        "SELECT link, title, date_found, source, image, summary, published FROM seen ORDER BY date_found DESC"
    ).fetchall()

    cards_html = []
    modals_html = []
    for i, (link, title, date_found, source, image, summary, published) in enumerate(rows):
        card_image = (
            f'<img src="{html_escape(image)}" alt="flyer" class="card-img">'
            if image else '<div class="card-img card-img-placeholder">🎓</div>'
        )
        cards_html.append(f"""
        <div class="card" onclick="document.getElementById('modal-{i}').style.display='flex'">
            {card_image}
            <div class="card-body">
                <h3>{html_escape(title)}</h3>
                <p class="card-source">{html_escape(source)}</p>
            </div>
        </div>
        """)

        modal_image = (
            f'<img src="{html_escape(image)}" alt="flyer" class="modal-img">' if image else ""
        )
        summary_text = html_escape(summary) if summary else "Aucun résumé disponible pour cette annonce — consulte le lien ci-dessous pour les critères complets et la procédure de candidature."
        modals_html.append(f"""
        <div id="modal-{i}" class="modal-overlay" onclick="if(event.target===this) this.style.display='none'">
            <div class="modal-box">
                <button class="modal-close" onclick="document.getElementById('modal-{i}').style.display='none'">✕</button>
                {modal_image}
                <h2>{html_escape(title)}</h2>
                <p class="modal-meta">Source : {html_escape(source)} · Trouvé le {html_escape(date_found[:10])}</p>
                <p class="modal-summary">{summary_text}</p>
                <a class="apply-btn" href="{html_escape(link)}" target="_blank" rel="noopener">
                    Voir l'annonce complète et postuler →
                </a>
            </div>
        </div>
        """)

    html_content = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Bourses Génie Civil - Tableau de bord</title>
<style>
    body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 24px; color: #1a1a1a; }}
    h1 {{ text-align: center; margin-bottom: 4px; }}
    .subtitle {{ text-align: center; color: #666; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 18px; max-width: 1200px; margin: 0 auto; }}
    .card {{ background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); cursor: pointer; transition: transform 0.15s; }}
    .card:hover {{ transform: translateY(-3px); box-shadow: 0 4px 14px rgba(0,0,0,0.12); }}
    .card-img {{ width: 100%; height: 150px; object-fit: cover; display: block; }}
    .card-img-placeholder {{ display: flex; align-items: center; justify-content: center; font-size: 40px; background: #e8edf3; }}
    .card-body {{ padding: 14px; }}
    .card-body h3 {{ font-size: 15px; margin: 0 0 6px 0; line-height: 1.3; }}
    .card-source {{ font-size: 12px; color: #888; margin: 0; }}
    .modal-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.55); align-items: center; justify-content: center; padding: 20px; z-index: 100; }}
    .modal-box {{ background: white; border-radius: 14px; max-width: 560px; width: 100%; max-height: 85vh; overflow-y: auto; padding: 24px; position: relative; }}
    .modal-img {{ width: 100%; max-height: 260px; object-fit: cover; border-radius: 8px; margin-bottom: 14px; }}
    .modal-close {{ position: absolute; top: 14px; right: 14px; border: none; background: #eee; border-radius: 50%; width: 32px; height: 32px; cursor: pointer; font-size: 16px; }}
    .modal-meta {{ color: #888; font-size: 13px; margin: 4px 0 14px 0; }}
    .modal-summary {{ line-height: 1.5; margin-bottom: 20px; }}
    .apply-btn {{ display: inline-block; background: #2563eb; color: white; padding: 10px 18px; border-radius: 8px; text-decoration: none; font-weight: 600; }}
    .apply-btn:hover {{ background: #1d4ed8; }}
</style>
</head>
<body>
    <h1>🎓 Bourses Génie Civil</h1>
    <p class="subtitle">{len(rows)} annonce(s) trouvée(s) au total — mis à jour le {datetime.now().strftime('%d/%m/%Y à %H:%M')}</p>
    <div class="grid">
        {''.join(cards_html)}
    </div>
    {''.join(modals_html)}
</body>
</html>
"""

    os.makedirs(os.path.dirname(DASHBOARD_PATH) or ".", exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[✓] Site web mis à jour : {DASHBOARD_PATH} ({len(rows)} annonce(s))")


# =========================== MAIN ===========================

def main():
    print(f"--- Scan lancé le {datetime.now().isoformat()} ---")
    conn = init_db()
    new_items = collect_new_scholarships(conn)
    send_email(new_items)
    send_telegram(new_items)
    send_whatsapp(new_items)
    generate_dashboard(conn)
    conn.close()
    print("--- Scan terminé ---")


if __name__ == "__main__":
    main()

