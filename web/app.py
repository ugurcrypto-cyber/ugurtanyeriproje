"""
Isi Kaybi Otomasyonu - Uyelikli web uygulamasi.

Musteri kayit olur, giris yapar, uyelik durumuna gore proje (DXF) yukler, hazir Excel'leri indirir.
Uyelik/paket yonetimi yonetici panelinden yapilir. Hesaplama motoru deploy klasorundeki dosyalardir.

Ortam degiskenleri:
  VERI_DIZINI      veritabani ve isler icin kalici klasor (varsayilan: deploy klasoru)
  WEB_GIZLI        oturum imza anahtari (verilmezse VERI_DIZINI altinda bir kez uretilir)
  ADMIN_EPOSTA / ADMIN_SIFRE   ilk yonetici hesabi (yoksa olusturulur)
  GUVENLI_CEREZ=1  HTTPS arkasinda calisirken oturum cerezini 'Secure' yapar (canli ortamda ZORUNLU)
  MAX_YUKLEME_MB   tek istekte toplam yukleme siniri (varsayilan 300)
  SAKLAMA_GUN      is dosyalarinin saklanma suresi (varsayilan 7 gun)
"""
import datetime
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

KOK = os.path.dirname(os.path.abspath(__file__))
MOTOR_DIZINI = os.path.dirname(KOK)          # deploy/: motor dosyalari burada
if MOTOR_DIZINI not in sys.path:
    sys.path.insert(0, MOTOR_DIZINI)

VERI = os.environ.get("VERI_DIZINI", "").strip() or MOTOR_DIZINI
os.makedirs(VERI, exist_ok=True)
DB_YOLU = os.path.join(VERI, "web.db")
IS_DIZINI = os.path.join(VERI, "web_isler")
os.makedirs(IS_DIZINI, exist_ok=True)

MAX_YUKLEME_MB = int(os.environ.get("MAX_YUKLEME_MB", "300"))
SAKLAMA_GUN = int(os.environ.get("SAKLAMA_GUN", "7"))
GUVENLI_CEREZ = os.environ.get("GUVENLI_CEREZ", "0") == "1"

PAKETLER = {
    "deneme": {"ad": "Deneme", "hak": 1, "gun": 7},
    "baslangic": {"ad": "Başlangıç", "hak": 15, "gun": 30},
    "ofis": {"ad": "Ofis", "hak": 40, "gun": 30},
}
BINA_FONKSIYONU = {
    "konut": "Konut (Apartman / Çok Daireli)",
    "konut_dukkan": "Konut + Dükkan (Karma Yapı)",
    "villa": "Villa / Müstakil Ev (Dış Hava Temaslı Giriş - DK)",
    "ofis": "Ofis",
    "ofis_dukkan": "Ofis + Dükkan",
}
YON_REFERANSI = {
    "yukari_kuzey": "Yüklenen plan açısına göre yukarısını KUZEY (0°) al (+Y=Kuzey, +X=Doğu, -Y=Güney, -X=Batı)",
    "sag_kuzey": "Sağ tarafı KUZEY al (+X=Kuzey, -Y=Doğu, -X=Güney, +Y=Batı)",
    "asagi_kuzey": "Aşağı tarafı KUZEY al (-Y=Kuzey, -X=Doğu, +Y=Güney, +X=Batı)",
    "sol_kuzey": "Sol tarafı KUZEY al (-X=Kuzey, +Y=Doğu, +X=Güney, -Y=Batı)",
}
UST_TAVAN = {
    "CC1": "Çatılı tavan - Plak Döşeme (CC1)",
    "CC2": "Çatılı tavan - Asmolen Döşeme (CC2)",
    "CA1": "Üzeri açık çatı (CA1)",
    "CA2": "Gezilemeyen teras / balkon - Plak (CA2)",
    "CA3": "Gezilemeyen teras / açık çatı - Asmolen (CA3)",
    "CA1_CA2": "(CA1 / CA2 / CA3) Üzeri açık çatı / Teras",
    "KARISIK": "Karışık (CA1 / CA2 / CA3 / CC1 / CC2)",
}
ZEMIN_TABAN = {
    "TO1": "Isıtılmayan mahal / dükkan üstü - Plak Döşeme (TO1)",
    "TO2": "Isıtılmayan mahal / dükkan üstü - Asmolen Döşeme (TO2)",
    "TG1": "Açık geçit / dış ortam üstü - Plak Döşeme (TG1)",
    "TG2": "Açık geçit / dış ortam üstü - Asmolen Döşeme (TG2)",
    "TT1": "Toprağa oturan döşeme (TT1)",
    "IKISI": "Hibrit / Karma (Plak TO1 + Asmolen TO2 / TG1-TG2 birlikte)",
}
DOSEME = {
    "plak": "Plak Döşeme (TO1, TG1, CC1, CA2 grubu ve DH3 kiriş aktif)",
    "asmolen": "Asmolen Döşeme (TO2, TG2, CC2, CA3 grubu aktif, DH3 = 0.00 pasif)",
    "karma": "Karma Döşeme: Zemin Plak (TO1/TG1) + Üst Katlar Asmolen (TO2/TG2/CC2/CA3)",
    "diger": "Diğer (Kirişli / Plak)",
}
BILESEN_SECENEKLERI = {
    "DH1": {"ad": "DH1 - Dış Duvar (Tuğla/Gazbeton)", "grup": "duvar", "varsayilan": True},
    "DH2": {"ad": "DH2 - Dış Kolon / Perde Duvar", "grup": "duvar", "varsayilan": True},
    "DH3": {"ad": "DH3 - Dış Kiriş (Plak Döşemede Aktif)", "grup": "duvar", "varsayilan": True},
    "DT1": {"ad": "DT1 - Toprağa Temaslı Dış/Perde Duvar", "grup": "duvar", "varsayilan": False},
    "DO1": {"ad": "DO1 - Isıtılmayan İç Ortama Komşu Duvar", "grup": "duvar", "varsayilan": True},
    "CC1": {"ad": "CC1 - Çatılı Tavan (Plak Döşeme)", "grup": "tavan", "varsayilan": True},
    "CC2": {"ad": "CC2 - Çatılı Tavan (Asmolen Döşeme)", "grup": "tavan", "varsayilan": False},
    "CA1": {"ad": "CA1 - Üzeri Açık Çatı", "grup": "tavan", "varsayilan": True},
    "CA2": {"ad": "CA2 - Gezilemeyen Teras / Balkon (Plak)", "grup": "tavan", "varsayilan": False},
    "CA3": {"ad": "CA3 - Gezilemeyen Teras / Açık Çatı (Asmolen)", "grup": "tavan", "varsayilan": False},
    "TT1": {"ad": "TT1 - Toprağa Oturan Döşeme", "grup": "taban", "varsayilan": False},
    "TO1": {"ad": "TO1 - Isıtılmayan Mahal Üstü Taban (Plak Döşeme)", "grup": "taban", "varsayilan": True},
    "TO2": {"ad": "TO2 - Isıtılmayan Mahal Üstü Taban (Asmolen Döşeme)", "grup": "taban", "varsayilan": False},
    "TG1": {"ad": "TG1 - Açık Geçit / Dış Ortam Üstü (Plak Döşeme)", "grup": "taban", "varsayilan": False},
    "TG2": {"ad": "TG2 - Açık Geçit / Dış Ortam Üstü (Asmolen Döşeme)", "grup": "taban", "varsayilan": False},
}

log = logging.getLogger("web")
logging.basicConfig(level=logging.INFO)


def _gizli_anahtar():
    env = os.environ.get("WEB_GIZLI", "").strip()
    if env:
        return env
    yol = os.path.join(VERI, "web_gizli_anahtar")
    if not os.path.exists(yol):
        with open(yol, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
    return open(yol, encoding="utf-8").read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_gizli_anahtar(),
    MAX_CONTENT_LENGTH=MAX_YUKLEME_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=GUVENLI_CEREZ,
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
)

# ----------------------------------------------------------------------------- veritabani
SEMA = """
create table if not exists kullanici(
  id integer primary key autoincrement,
  eposta text unique not null,
  ad text not null,
  sifre text not null,
  rol text not null default 'musteri',
  aktif integer not null default 1,
  paket text,
  hak integer not null default 0,
  bitis text,
  olusturma text not null
);
create table if not exists is_kaydi(
  id text primary key,
  kullanici_id integer not null,
  ad text not null,
  parametreler text not null,
  durum text not null,
  mesaj text,
  dosyalar text,
  olusturma text not null,
  bitis text
);
"""


def baglan():
    con = sqlite3.connect(DB_YOLU, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def db():
    if "db" not in g:
        g.db = baglan()
    return g.db


@app.teardown_appcontext
def _kapat(_e):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def simdi():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db_hazirla():
    con = baglan()
    con.executescript(SEMA)
    eposta = os.environ.get("ADMIN_EPOSTA", "").strip().lower()
    sifre = os.environ.get("ADMIN_SIFRE", "")
    if eposta and sifre:
        if not con.execute("select 1 from kullanici where eposta=?", (eposta,)).fetchone():
            con.execute("insert into kullanici(eposta, ad, sifre, rol, aktif, olusturma) values(?,?,?,?,1,?)",
                        (eposta, "Yönetici", generate_password_hash(sifre), "admin", simdi()))
    con.commit()
    con.close()


# ----------------------------------------------------------------------------- guvenlik yardimcilari
def csrf_belirteci():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.context_processor
def _sablon_yardimcilari():
    return {
        "csrf_input": lambda: Markup(f'<input type="hidden" name="csrf" value="{csrf_belirteci()}">'),
        "kullanici": mevcut_kullanici(),
        "PAKETLER": PAKETLER,
        "SAKLAMA_GUN": SAKLAMA_GUN,
    }


@app.before_request
def _csrf_kontrol():
    if request.method == "POST":
        gelen = request.form.get("csrf", "")
        beklenen = session.get("csrf", "")
        if not beklenen or not secrets.compare_digest(gelen, beklenen):
            abort(400, "Oturum doğrulaması başarısız. Sayfayı yenileyip tekrar deneyin.")


@app.after_request
def _basliklar(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; form-action 'self'"
    return resp


_BASARISIZ = {}
_BASARISIZ_KILIT = threading.Lock()


def _giris_kisitli_mi(anahtar):
    with _BASARISIZ_KILIT:
        t = time.time()
        liste = [x for x in _BASARISIZ.get(anahtar, []) if t - x < 600]
        _BASARISIZ[anahtar] = liste
        return len(liste) >= 5


def _basarisiz_ekle(anahtar):
    with _BASARISIZ_KILIT:
        _BASARISIZ.setdefault(anahtar, []).append(time.time())


def mevcut_kullanici():
    if "kid" not in session:
        return None
    if "_kullanici" not in g:
        g._kullanici = db().execute("select * from kullanici where id=? and aktif=1", (session["kid"],)).fetchone()
    return g._kullanici


def giris_gerekli(f):
    @wraps(f)
    def sar(*a, **k):
        if not mevcut_kullanici():
            return redirect(url_for("giris"))
        return f(*a, **k)
    return sar


def yonetici_gerekli(f):
    @wraps(f)
    def sar(*a, **k):
        u = mevcut_kullanici()
        if not u:
            return redirect(url_for("giris"))
        if u["rol"] != "admin":
            abort(403)
        return f(*a, **k)
    return sar


def uyelik_durumu(u):
    """(izinli_mi, mesaj)"""
    if not u["paket"]:
        return False, "Üyeliğiniz henüz aktif edilmedi. Paket tanımlanması için yönetici ile iletişime geçin."
    if u["bitis"] and datetime.date.today() > datetime.date.fromisoformat(u["bitis"]):
        return False, f"Üyeliğiniz {u['bitis']} tarihinde sona erdi."
    if int(u["hak"]) <= 0:
        return False, "Proje hakkınız bitti. Yeni paket için yönetici ile iletişime geçin."
    return True, ""


# ----------------------------------------------------------------------------- motor
def _detay_kaydet(cikti, tum_mahaller, uyarilar=None, yeni_katmanlar=None):
    detay = {
        "mahal_sayisi": len(tum_mahaller),
        "toplam_dh1": round(sum(float(m.get("dh1", 0.0)) for m in tum_mahaller), 2),
        "toplam_dh2": round(sum(float(m.get("dh2", 0.0)) for m in tum_mahaller), 2),
        "toplam_dh3": round(sum(float(m.get("dh3", 0.0)) for m in tum_mahaller), 2),
        "toplam_do1": round(sum(float(m.get("do1", 0.0)) for m in tum_mahaller), 2),
        "uyarilar": uyarilar or [],
        "yeni_katmanlar": yeni_katmanlar or {},
        "mahaller": [
            {
                "blok": m.get("blok", "PROJE"),
                "code": m["code"],
                "name": m["name"],
                "temp": m.get("temp", 20),
                "area": m.get("area", 0.0),
                "dh1": round(float(m.get("dh1", 0.0)), 2),
                "dh2": round(float(m.get("dh2", 0.0)), 2),
                "dh3": round(float(m.get("dh3", 0.0)), 2),
                "do1": round(float(m.get("do1", 0.0)), 2),
                "ceil_code": m.get("ceil_code", ""),
                "ceil_area": round(float(m.get("ceil_area", 0.0) or 0.0), 2),
                "ceil2_code": m.get("ceil2_code", ""),
                "ceil2_area": round(float(m.get("ceil2_area", 0.0) or 0.0), 2),
                "floor_code": m.get("floor_code", ""),
                "floor_area": round(float(m.get("floor_area", 0.0) or 0.0), 2),
                "floor2_code": m.get("floor2_code", ""),
                "floor2_area": round(float(m.get("floor2_area", 0.0) or 0.0), 2),
                "yon": round(float(m.get("yon", 0.0) or 0.0), 1),
                "ik": f"İK:{m['ik'][0]}x{m['ik'][1]}" if m.get("ik") and float(m["ik"][0]) > 0 else "",
                "wins": [f"{w[0]}:{w[1]}x{w[2]}" for w in m.get("wins", []) if float(w[1]) > 0],
            }
            for m in tum_mahaller
        ],
    }
    with open(os.path.join(cikti, "sonuc_detay.json"), "w", encoding="utf-8") as f:
        json.dump(detay, f, ensure_ascii=False, indent=2)


def motor_calistir(girdi, cikti, parametreler):
    """(basarili, mesaj, [dosya adlari]). Dogrulanmis proje veya olculu yeni DXF projeleri icin Excel uretir."""
    import importlib
    import katman_kutuphanesi
    import dxf_okuyucu
    import dxf_olcu_okuyucu
    import excel_yazici_v2
    import proje_kapisi
    importlib.reload(katman_kutuphanesi)
    importlib.reload(dxf_okuyucu)
    importlib.reload(dxf_olcu_okuyucu)
    importlib.reload(excel_yazici_v2)
    importlib.reload(proje_kapisi)
    from api_sunucu import DEFAULT_TEMPLATE
    from excel_yazici_v2 import generate_xlsm_pure_python
    from dwg_excel_otomasyon_calisma import BLOCK_CONFIGS, build_all_floors_for_block
    from dxf_olcu_okuyucu import pilot_veya_dinamik_hesapla

    ok, mesaj = proje_kapisi.kontrol(girdi, test_modu=False)
    if ok:
        dosyalar = []
        tum_mahaller = []
        asmolen_mi = str((parametreler or {}).get("doseme", "")).strip().lower() == "asmolen"
        for blok, cfg in BLOCK_CONFIGS.items():
            mahaller = build_all_floors_for_block(cfg)
            if asmolen_mi:
                for m in mahaller:
                    m["doseme"] = "asmolen"
                    m["dh3"] = 0.0
            generate_xlsm_pure_python(DEFAULT_TEMPLATE, os.path.join(cikti, cfg["filename"]), blok, mahaller)
            dosyalar.append(cfg["filename"])
            for m in mahaller:
                tum_mahaller.append({**m, "blok": blok})
        _detay_kaydet(cikti, tum_mahaller)
        return True, f"{len(dosyalar)} blok ({len(tum_mahaller)} mahal) için Excel hazırlandı.", dosyalar

    # Eger Sumru degilse ama gecerli MAHAL NO katmani varsa dinamik olcu okuyucu ile uret
    dxfler = [os.path.join(girdi, d) for d in os.listdir(girdi) if d.lower().endswith(".dxf")]
    if dxfler and any(proje_kapisi._mahal_no_katmani_var_mi(p) for p in dxfler):
        try:
            sonuc = pilot_veya_dinamik_hesapla(dxfler, parametreler)
            mahaller = sonuc["mahaller"]
            if mahaller:
                cikti_adi = "Klima Hesap Tablosu - Isi Kaybi.xlsm"
                generate_xlsm_pure_python(DEFAULT_TEMPLATE, os.path.join(cikti, cikti_adi), "PROJE", mahaller)
                _detay_kaydet(cikti, [{**m, "blok": "PROJE"} for m in mahaller], sonuc.get("uyarilar"), sonuc.get("yeni_katmanlar"))
                return True, f"{len(mahaller)} mahal için DH1/DH2/DH3 hesaplandı ve Excel hazırlandı.", [cikti_adi]
        except Exception:
            log.exception("dinamik olcu okuyucu hatasi")

    return False, mesaj, []


_ISCI = ThreadPoolExecutor(max_workers=1)   # ağır işleri sırayla çalıştırır (bellek koruması)


def isle(is_id):
    con = baglan()
    try:
        r = con.execute("select * from is_kaydi where id=?", (is_id,)).fetchone()
        if not r:
            return
        con.execute("update is_kaydi set durum='isleniyor' where id=?", (is_id,))
        con.commit()
        girdi = os.path.join(IS_DIZINI, is_id, "girdi")
        cikti = os.path.join(IS_DIZINI, is_id, "cikti")
        os.makedirs(cikti, exist_ok=True)
        ok, mesaj, dosyalar = motor_calistir(girdi, cikti, json.loads(r["parametreler"]))
        if ok:
            con.execute("update kullanici set hak = hak - 1 where id=? and hak > 0", (r["kullanici_id"],))
        con.execute("update is_kaydi set durum=?, mesaj=?, dosyalar=?, bitis=? where id=?",
                    ("tamam" if ok else "hata", mesaj, json.dumps(dosyalar, ensure_ascii=False), simdi(), is_id))
        con.commit()
    except Exception:
        log.exception("is basarisiz: %s", is_id)
        con.execute("update is_kaydi set durum='hata', mesaj=?, bitis=? where id=?",
                    ("Beklenmeyen bir hata oluştu. Dosyaları kontrol edip tekrar deneyin.", simdi(), is_id))
        con.commit()
    finally:
        con.close()


def eski_isleri_temizle():
    sinir = time.time() - SAKLAMA_GUN * 86400
    con = baglan()
    try:
        for d in os.listdir(IS_DIZINI):
            yol = os.path.join(IS_DIZINI, d)
            if os.path.isdir(yol) and os.path.getmtime(yol) < sinir:
                shutil.rmtree(yol, ignore_errors=True)
                con.execute("update is_kaydi set durum='silindi', dosyalar='[]', "
                            "mesaj='Dosyalar saklama süresi dolduğu için silindi.' where id=? and durum in ('tamam','hata')", (d,))
        con.commit()
    finally:
        con.close()


def _temizlik_dongusu():
    while True:
        try:
            eski_isleri_temizle()
        except Exception:
            log.exception("temizlik hatasi")
        time.sleep(3600)


def baslat():
    """Veritabanini hazirlar; yarim kalan isleri toparlar; temizlik is parcacigini baslatir."""
    db_hazirla()
    con = baglan()
    con.execute("update is_kaydi set durum='hata', mesaj='Sunucu yeniden başlatıldığı için iş yarıda kaldı. Lütfen tekrar yükleyin.' "
                "where durum='isleniyor'")
    bekleyen = [r["id"] for r in con.execute("select id from is_kaydi where durum='kuyrukta' order by olusturma")]
    con.commit()
    con.close()
    for i in bekleyen:
        _ISCI.submit(isle, i)
    threading.Thread(target=_temizlik_dongusu, daemon=True).start()


# ----------------------------------------------------------------------------- sayfalar
EPOSTA_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/")
def ana():
    return redirect(url_for("panel" if mevcut_kullanici() else "giris"))


@app.route("/durum")
def durum():
    return {"durum": "aktif"}


@app.route("/kayit", methods=["GET", "POST"])
def kayit():
    if request.method == "POST":
        ad = request.form.get("ad", "").strip()[:80]
        eposta = request.form.get("eposta", "").strip().lower()[:120]
        sifre = request.form.get("sifre", "")
        if not ad or not EPOSTA_RE.match(eposta):
            flash("Ad soyad ve geçerli bir e-posta girin.", "hata")
        elif len(sifre) < 8:
            flash("Şifre en az 8 karakter olmalı.", "hata")
        elif db().execute("select 1 from kullanici where eposta=?", (eposta,)).fetchone():
            flash("Bu e-posta ile zaten bir hesap var.", "hata")
        else:
            db().execute("insert into kullanici(eposta, ad, sifre, olusturma) values(?,?,?,?)",
                         (eposta, ad, generate_password_hash(sifre), simdi()))
            db().commit()
            flash("Kaydınız alındı. Üyeliğiniz yönetici tarafından aktif edildiğinde proje yükleyebilirsiniz. Şimdi giriş yapabilirsiniz.", "ok")
            return redirect(url_for("giris"))
    return render_template("kayit.html")


@app.route("/giris", methods=["GET", "POST"])
def giris():
    if request.method == "POST":
        eposta = request.form.get("eposta", "").strip().lower()
        sifre = request.form.get("sifre", "")
        anahtar = (request.remote_addr or "", eposta)
        if _giris_kisitli_mi(anahtar):
            flash("Çok fazla başarısız deneme. 10 dakika sonra tekrar deneyin.", "hata")
            return render_template("giris.html"), 429
        u = db().execute("select * from kullanici where eposta=? and aktif=1", (eposta,)).fetchone()
        if u and check_password_hash(u["sifre"], sifre):
            session.clear()
            session.permanent = True
            session["kid"] = u["id"]
            csrf_belirteci()
            return redirect(url_for("panel"))
        _basarisiz_ekle(anahtar)
        flash("E-posta veya şifre hatalı.", "hata")
    return render_template("giris.html")


@app.route("/cikis", methods=["POST"])
def cikis():
    session.clear()
    return redirect(url_for("giris"))


@app.route("/panel")
@giris_gerekli
def panel():
    u = mevcut_kullanici()
    isler = db().execute("select * from is_kaydi where kullanici_id=? order by olusturma desc limit 50", (u["id"],)).fetchall()
    ok, mesaj = uyelik_durumu(u)
    return render_template("panel.html", isler=isler, uyelik_ok=ok, uyelik_mesaj=mesaj, PAKET_ADI=PAKETLER.get(u["paket"] or "", {}).get("ad", "-"))


def _bekleyen_is_sayisi(kid):
    return db().execute("select count(*) c from is_kaydi where kullanici_id=? and durum in ('kuyrukta','isleniyor')", (kid,)).fetchone()["c"]


@app.route("/yeni", methods=["GET", "POST"])
@giris_gerekli
def yeni():
    u = mevcut_kullanici()
    ok, mesaj = uyelik_durumu(u)
    if not ok:
        flash(mesaj, "hata")
        return redirect(url_for("panel"))
    if request.method == "POST":
        if int(u["hak"]) <= _bekleyen_is_sayisi(u["id"]):
            flash("Kalan hakkınız, sırada bekleyen işlerinizi karşılıyor. Önce onların bitmesini bekleyin.", "hata")
            return redirect(url_for("panel"))
        ad = request.form.get("proje_adi", "").strip()[:80]
        try:
            kat = int(request.form.get("kat_sayisi", ""))
        except ValueError:
            kat = 0
        bina_fonksiyonu = request.form.get("bina_fonksiyonu", "konut").strip() or "konut"
        yon_referansi = request.form.get("yon_referansi", "yukari_kuzey").strip() or "yukari_kuzey"
        doseme = request.form.get("doseme", "plak").strip() or "plak"
        aktif_bilesenler = [b.strip().upper() for b in request.form.getlist("aktif_bilesenler") if b.strip().upper() in BILESEN_SECENEKLERI]
        ust = request.form.get("ust_tavan", "").strip()
        taban = request.form.get("zemin_taban", "").strip()
        if not ust:
            if "CC2" in aktif_bilesenler and "CC1" not in aktif_bilesenler:
                ust = "CC2"
            elif "CA1" in aktif_bilesenler and "CC1" not in aktif_bilesenler and "CC2" not in aktif_bilesenler:
                ust = "CA1"
            elif "CA3" in aktif_bilesenler and "CC1" not in aktif_bilesenler:
                ust = "CA3"
            elif "CA2" in aktif_bilesenler and "CC1" not in aktif_bilesenler:
                ust = "CA2"
            else:
                ust = "KARISIK"
        if not taban:
            if "TO1" in aktif_bilesenler and "TO2" in aktif_bilesenler:
                taban = "IKISI"
            elif "TO2" in aktif_bilesenler:
                taban = "TO2"
            elif "TT1" in aktif_bilesenler and "TO1" not in aktif_bilesenler:
                taban = "TT1"
            elif "TG1" in aktif_bilesenler and "TO1" not in aktif_bilesenler:
                taban = "TG1"
            elif "TG2" in aktif_bilesenler and "TO2" not in aktif_bilesenler:
                taban = "TG2"
            else:
                taban = "TO1"
        aciklama = request.form.get("aciklama", "").strip()[:2500]
        dosyalar = [f for f in request.files.getlist("dosyalar") if f and f.filename]
        hatalar = []
        if not ad:
            hatalar.append("Proje adı gerekli.")
        if not 1 <= kat <= 40:
            hatalar.append("Kat sayısı 1 ile 40 arasında olmalı.")
        if bina_fonksiyonu not in BINA_FONKSIYONU or yon_referansi not in YON_REFERANSI:
            hatalar.append("Bina fonksiyonu ve yön referansı geçersiz.")
        if doseme not in DOSEME:
            hatalar.append("Ana döşeme kuralını seçin.")
        if not aktif_bilesenler:
            hatalar.append("En az bir ısı kaybı bileşeni (DH1, TO1, TO2, CC1, CA1 vb.) seçin.")
        if not dosyalar:
            hatalar.append("En az bir DXF dosyası seçin.")
        for f in dosyalar:
            n = f.filename.lower()
            if n.endswith(".dwg"):
                hatalar.append(f"'{f.filename}' DWG biçiminde. Lütfen AutoCAD'de DXF olarak kaydedip yükleyin.")
            elif not n.endswith(".dxf"):
                hatalar.append(f"'{f.filename}' desteklenmiyor. Yalnızca .dxf dosyaları yüklenebilir.")
        if hatalar:
            for h in hatalar:
                flash(h, "hata")
            return render_template("yeni.html", BINA_FONKSIYONU=BINA_FONKSIYONU, YON_REFERANSI=YON_REFERANSI,
                                   UST_TAVAN=UST_TAVAN, ZEMIN_TABAN=ZEMIN_TABAN, DOSEME=DOSEME,
                                   BILESEN_SECENEKLERI=BILESEN_SECENEKLERI, f=request.form,
                                   secili_bilesenler=aktif_bilesenler), 400
        is_id = uuid.uuid4().hex
        girdi = os.path.join(IS_DIZINI, is_id, "girdi")
        os.makedirs(girdi, exist_ok=True)
        for i, f in enumerate(dosyalar, 1):
            temiz = secure_filename(f.filename) or f"dosya{i}.dxf"
            if not temiz.lower().endswith(".dxf"):
                temiz += ".dxf"
            f.save(os.path.join(girdi, f"{i:02d}_{temiz}"))
        parametreler = {
            "kat_sayisi": kat,
            "bina_fonksiyonu": bina_fonksiyonu,
            "yon_referansi": yon_referansi,
            "ust_tavan": ust,
            "zemin_taban": taban,
            "doseme": doseme,
            "aktif_bilesenler": aktif_bilesenler,
            "aciklama": aciklama,
        }
        db().execute("insert into is_kaydi(id, kullanici_id, ad, parametreler, durum, olusturma) values(?,?,?,?,?,?)",
                     (is_id, u["id"], ad, json.dumps(parametreler, ensure_ascii=False), "kuyrukta", simdi()))
        db().commit()
        _ISCI.submit(isle, is_id)
        return redirect(url_for("is_detay", is_id=is_id))
    varsayilan_secili = [k for k, v in BILESEN_SECENEKLERI.items() if v["varsayilan"]]
    return render_template("yeni.html", BINA_FONKSIYONU=BINA_FONKSIYONU, YON_REFERANSI=YON_REFERANSI,
                           UST_TAVAN=UST_TAVAN, ZEMIN_TABAN=ZEMIN_TABAN, DOSEME=DOSEME,
                           BILESEN_SECENEKLERI=BILESEN_SECENEKLERI, f={},
                           secili_bilesenler=varsayilan_secili)


def _kendi_isim_veya_404(is_id):
    u = mevcut_kullanici()
    r = db().execute("select * from is_kaydi where id=?", (is_id,)).fetchone()
    if not r or (r["kullanici_id"] != u["id"] and u["rol"] != "admin"):
        abort(404)
    return r


@app.route("/is/<is_id>")
@giris_gerekli
def is_detay(is_id):
    r = _kendi_isim_veya_404(is_id)
    detay = None
    detay_yolu = os.path.join(IS_DIZINI, is_id, "cikti", "sonuc_detay.json")
    if os.path.isfile(detay_yolu):
        try:
            with open(detay_yolu, "r", encoding="utf-8") as f:
                detay = json.load(f)
        except Exception:
            detay = None
    return render_template("is.html", r=r, dosyalar=json.loads(r["dosyalar"] or "[]"), p=json.loads(r["parametreler"]),
                           detay=detay, BINA_FONKSIYONU=BINA_FONKSIYONU, YON_REFERANSI=YON_REFERANSI,
                           UST_TAVAN=UST_TAVAN, ZEMIN_TABAN=ZEMIN_TABAN, DOSEME=DOSEME)


@app.route("/is/<is_id>/revize", methods=["POST"])
@giris_gerekli
def is_revize(is_id):
    """Kullanıcının tablo üzerinde satır satır yaptığı hibrit tavan/taban (CA1/CA2/CC1, TO1/TG1/TT1) ve metraj müdahalelerini kaydedip Excel'i yeniler."""
    r = _kendi_isim_veya_404(is_id)
    parametreler = json.loads(r["parametreler"] or "{}")
    mahal_ozel = {}
    kodlar = request.form.getlist("kod")
    for k in kodlar:
        k = k.strip()
        if not k:
            continue
        try:
            mahal_ozel[k] = {
                "dh1": float(request.form.get(f"dh1_{k}", 0) or 0),
                "dh2": float(request.form.get(f"dh2_{k}", 0) or 0),
                "dh3": float(request.form.get(f"dh3_{k}", 0) or 0),
                "do1": float(request.form.get(f"do1_{k}", 0) or 0),
                "ceil_code": request.form.get(f"ceil_code_{k}", "CA1").strip().upper(),
                "ceil_area": float(request.form.get(f"ceil_area_{k}", 0) or 0),
                "ceil2_code": request.form.get(f"ceil2_code_{k}", "").strip().upper(),
                "ceil2_area": float(request.form.get(f"ceil2_area_{k}", 0) or 0),
                "floor_code": request.form.get(f"floor_code_{k}", "TO1").strip().upper(),
                "floor_area": float(request.form.get(f"floor_area_{k}", 0) or 0),
                "floor2_code": request.form.get(f"floor2_code_{k}", "").strip().upper(),
                "floor2_area": float(request.form.get(f"floor2_area_{k}", 0) or 0),
            }
        except ValueError:
            continue
    parametreler["mahal_ozel"] = mahal_ozel
    girdi = os.path.join(IS_DIZINI, is_id, "girdi")
    cikti = os.path.join(IS_DIZINI, is_id, "cikti")
    ok, mesaj, dosyalar = motor_calistir(girdi, cikti, parametreler)
    db().execute("update is_kaydi set parametreler=?, durum=?, mesaj=?, dosyalar=?, bitis=? where id=?",
                 (json.dumps(parametreler, ensure_ascii=False), "tamam" if ok else "hata", mesaj,
                  json.dumps(dosyalar, ensure_ascii=False), simdi(), is_id))
    db().commit()
    flash("Hibrit tavan/taban ve metraj değişiklikleri uygulanarak Excel yeniden üretildi.", "ok")
    return redirect(url_for("is_detay", is_id=is_id))


@app.route("/is/<is_id>/durum.json")
@giris_gerekli
def is_durum_json(is_id):
    r = _kendi_isim_veya_404(is_id)
    detay = None
    detay_yolu = os.path.join(IS_DIZINI, is_id, "cikti", "sonuc_detay.json")
    if os.path.isfile(detay_yolu):
        try:
            with open(detay_yolu, "r", encoding="utf-8") as f:
                detay = json.load(f)
        except Exception:
            detay = None
    return {
        "id": r["id"],
        "durum": r["durum"],
        "mesaj": r["mesaj"],
        "dosyalar": json.loads(r["dosyalar"] or "[]"),
        "detay": detay,
    }


@app.route("/indir/<is_id>/<int:sira>")
@giris_gerekli
def indir(is_id, sira):
    r = _kendi_isim_veya_404(is_id)
    dosyalar = json.loads(r["dosyalar"] or "[]")
    if r["durum"] != "tamam" or not 0 <= sira < len(dosyalar):
        abort(404)
    yol = os.path.join(IS_DIZINI, is_id, "cikti", dosyalar[sira])
    if not os.path.isfile(yol):
        abort(404)
    return send_file(yol, as_attachment=True, download_name=dosyalar[sira])


@app.route("/admin")
@yonetici_gerekli
def admin():
    kullanicilar = db().execute(
        "select k.*, (select count(*) from is_kaydi i where i.kullanici_id=k.id) as is_sayisi from kullanici k order by k.olusturma desc").fetchall()
    return render_template("admin.html", kullanicilar=kullanicilar)


@app.route("/admin/kullanici/<int:kid>", methods=["POST"])
@yonetici_gerekli
def admin_kullanici(kid):
    paket = request.form.get("paket", "")
    aktif = 1 if request.form.get("aktif") else 0
    if paket and paket not in PAKETLER:
        abort(400)
    if request.form.get("varsayilan") and paket:
        hak = PAKETLER[paket]["hak"]
        bitis = (datetime.date.today() + datetime.timedelta(days=PAKETLER[paket]["gun"])).isoformat()
    else:
        try:
            hak = max(0, int(request.form.get("hak", "0")))
        except ValueError:
            abort(400)
        bitis = request.form.get("bitis", "").strip() or None
        if bitis:
            try:
                datetime.date.fromisoformat(bitis)
            except ValueError:
                abort(400)
    if kid == mevcut_kullanici()["id"]:
        aktif = 1  # yonetici kendi hesabini kapatamaz
    db().execute("update kullanici set paket=?, hak=?, bitis=?, aktif=? where id=?", (paket or None, hak, bitis, aktif, kid))
    db().commit()
    flash("Üyelik güncellendi.", "ok")
    return redirect(url_for("admin"))


@app.errorhandler(413)
def _cok_buyuk(_e):
    flash(f"Yükleme çok büyük (en fazla {MAX_YUKLEME_MB} MB).", "hata")
    return redirect(url_for("yeni")) if mevcut_kullanici() else redirect(url_for("giris"))


baslat()

if __name__ == "__main__":
    ana_port = int(os.environ.get("WEB_PORT", "8080"))
    if ana_port != 80:
        def _port80_dinle():
            try:
                app.run(host="0.0.0.0", port=80, debug=False, use_reloader=False, threaded=True)
            except Exception:
                pass
        threading.Thread(target=_port80_dinle, daemon=True).start()
    app.run(host="0.0.0.0", port=ana_port, debug=False, use_reloader=False, threaded=True)
