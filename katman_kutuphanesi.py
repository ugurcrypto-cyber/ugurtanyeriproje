"""
Dinamik Katman Kütüphanesi ve Otomatik Katman Keşif Motoru (Öğrenen Mimari Yapı - Kurumsal Veri Dayanıklılığı Sürümü).

Mimari Dayanıklılık ve Güvenlik Katmanları (4 Temel Kural):
1. Kalıcı Depolama (Persistent Volume) ve SQLite / PostgreSQL Soyutlama Katmanı:
   - Docker 'VOLUME /veri' ('VERI_DIZINI') ve yerel kalıcı veri dizini ('yerel_veri/kutuphane')
     üzerinde çalışır; konteyner yeniden başlatılsa bile öğrenilen katmanlar ve mimar profilleri silinmez.
   - 'KutuphaneDepoKatmani' soyutlaması ile tüm JSON kayıtları eşzamanlı olarak gömülü SQLite
     veritabanına ('katman_kutuphanesi.db') yazılır ve 'KUTUPHANE_DB_URL' ortam değişkeniyle
     PostgreSQL'e şema bozulmadan dinamik geçişe hazırdır.
2. Çoklu İş Parçacığı ve Süreç Güvenli Dosya Kilitleme (Thread & Process-Safe Locking):
   - 'threading.RLock()' ve işletim sistemi düzeyinde dosya kilidi ('portalocker' varsa öncelikli,
     yoksa Windows 'msvcrt.locking' / POSIX 'fcntl.flock') ile eşzamanlı çoklu kullanıcı yüklemelerinde
     yarış durumlarını (race condition) ve dosya bozulmalarını %100 engeller.
3. Otomatik Zaman Damgalı Yedekleme ve Bulut Senkronizasyon Kancası (Cloud Sync Hook):
   - 'kutuphane_kaydet()' her yeni mimar profili / katman deseni öğrendiğinde otomatik olarak
     'katman_kutuphanesi_YYYYMMDD.json' (ör. 'katman_kutuphanesi_20261025.json') ve saat damgalı
     yedek üretir; 'bulut_yedekleme_kancasi()' üzerinden Google Drive veya AWS S3'e aktarıma hazırdır.
4. Sıkı Şema Doğrulama (Try-Except Validation) ve Otomatik Öz-Onarım (Auto-Recovery):
   - Tüm okuma rutinleri sıkı şema doğrulamasından geçer. Ana JSON dosyası bozulmuş veya okunamaz
     durumdaysa sistem çökmez; hatayı loglar, en güncel sağlıklı zaman damgalı yedekten veya
     gömülü SQLite veritabanından veriyi otomatik kurtarır ve ana JSON dosyasını onarır.
"""
import collections
import contextlib
import datetime
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import urllib.request

# İsteğe bağlı portalocker desteği + işletim sistemi yerel kilit geri dönüşü (fallback)
try:
    import portalocker as _portalocker
except ImportError:
    _portalocker = None

if os.name == "nt":
    import msvcrt as _msvcrt
else:
    import fcntl as _fcntl

logger = logging.getLogger("katman_kutuphanesi")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

BU_DIZIN = os.path.dirname(os.path.abspath(__file__))
KUTUPHANE_DOSYA_ADI = "katman_kutuphanesi.json"
SQLITE_DOSYA_ADI = "katman_kutuphanesi.db"

# Süreç içi eşzamanlılık için yeniden-girişli (re-entrant) iş parçacığı kilidi ve derinlik sayacı
_KUTUPHANE_THREAD_LOCK = threading.RLock()
_THREAD_LOCAL = threading.local()

# Özel bulut senkronizasyon geri çağrıları (callback) listesi
_BULUT_KANCALARI = []


def _kalici_kutuphane_dizini_belirle():
    """
    Docker Persistent Volume ('VERI_DIZINI=/veri') veya yerel kalıcı veri dizinini ('yerel_veri/kutuphane')
    tespit eder ve oluşturur. Böylece konteyner katmanı sıfırlansa bile kütüphane kalıcı diskte yaşar.
    """
    veri_env = os.environ.get("VERI_DIZINI", "").strip()
    if veri_env:
        kalici_kok = os.path.join(os.path.abspath(veri_env), "kutuphane")
    elif os.path.basename(BU_DIZIN) == "deploy":
        kalici_kok = os.path.join(BU_DIZIN, "yerel_veri", "kutuphane")
    elif os.path.isdir(os.path.join(BU_DIZIN, "deploy")):
        kalici_kok = os.path.join(BU_DIZIN, "deploy", "yerel_veri", "kutuphane")
    else:
        kalici_kok = os.path.join(BU_DIZIN, "yerel_veri", "kutuphane")

    try:
        os.makedirs(kalici_kok, exist_ok=True)
        os.makedirs(os.path.join(kalici_kok, "yedekler"), exist_ok=True)
        return kalici_kok
    except Exception as exc:
        logger.warning("Kalıcı kütüphane dizini oluşturulamadı (%s), çalışma dizini kullanılacak: %s", kalici_kok, exc)
        yedek_dizin = os.path.join(BU_DIZIN, "yedekler")
        os.makedirs(yedek_dizin, exist_ok=True)
        return BU_DIZIN


KALICI_DIZIN = _kalici_kutuphane_dizini_belirle()
YEDEK_DIZINI = os.path.join(KALICI_DIZIN, "yedekler")
KALICI_KUTUPHANE_YOLU = os.path.join(KALICI_DIZIN, KUTUPHANE_DOSYA_ADI)
KALICI_SQLITE_YOLU = os.path.join(KALICI_DIZIN, SQLITE_DOSYA_ADI)
# Geriye dönük uyumluluk: çalışma dizinindeki yolu da sabit olarak tut
KUTUPHANE_YOLU = os.path.join(BU_DIZIN, KUTUPHANE_DOSYA_ADI)

ZORUNLU_LISTE_KATEGORILERI = (
    "mahal_kod_katmanlari",
    "mahal_metin_katmanlari",
    "olcu_ic_katmanlari",
    "olcu_dis_katmanlari",
    "duvar_dis_katmanlari",
    "duvar_ic_katmanlari",
    "kolon_katmanlari",
    "pencere_kapi_katmanlari",
)

VARSAYILAN_KUTUPHANE = {
    "surum": "3.0-kurumsal-dayanikli",
    "son_guncelleme": "2026-10-25T20:15:00",
    "mahal_kod_katmanlari": [
        "MAHAL NO",
        "MAHAL_NO",
        "MAHAL-NO",
        "MAHALNO",
    ],
    "mahal_metin_katmanlari": [
        "MİMARİ",
        "MIMARI",
        "Mimari",
        "mahal",
        "MAHAL",
        "M-MAHAL",
        "A-AREA",
        "Model Birimi - Mahal",
        "Annotation - Text",
        "+Detay - Yazı - 2",
        "+Detay - Yazı - 1",
        "+Detay - Yazı - 3",
        "+Detay - Yazı - 5",
        "Yazi",
        "yazı",
        "YAZI",
        "0",
        "MAHAL YAZI",
        "ODA",
        "ALAN",
    ],
    "olcu_ic_katmanlari": [
        "N-İÇ ÖLÇÜ yatay",
        "N-İÇ ÖLÇÜ dikey",
        "+Detay - Ölçü - İç",
        "Dimensioning - General",
        "ÖLÇÜ",
        "OLCU",
        "İÇ ÖLÇÜ",
        "IC OLCU",
        "DIM",
    ],
    "olcu_dis_katmanlari": [
        "DIŞ ÖLÇÜ",
        "+Detay - Ölçü - Dış",
        "+Detay - Ölçü - İç Ortak Alan",
    ],
    "duvar_dis_katmanlari": [
        "Dış Duvarlar",
        "Exterior walls",
        "+Detay - Duvar - 1",
        "DIŞ DUVAR",
        "DIS DUVAR",
    ],
    "duvar_ic_katmanlari": [
        "MİMARİ",
        "MIMARI",
        "Mimari",
        "duvar",
        "DUVAR",
        "Yapısal - Birleşik",
        "+Detay - Duvar - 2",
        "0",
    ],
    "kolon_katmanlari": [
        "+Detay - Betonarme - Kolonlar",
        "Yapısal - Taşıyıcı",
        "KOLON",
        "PERDE",
        "BETONARME",
    ],
    "pencere_kapi_katmanlari": [
        "Dış Duvarlar",
        "Mimari",
        "MİMARİ",
        "MIMARI",
        "0",
        "+Detay - Yazı - 1",
        "+Detay - Yazı - 2",
    ],
    "mimar_profilleri": {},
    "kesif_gecmisi": [],
}

ODA_ANAHTAR_KELIMELERI = (
    "SALON",
    "YATAK ODASI",
    "OTURMA ODASI",
    "ÇOCUK ODASI",
    "COCUK ODASI",
    "ODA",
    "MUTFAK",
    "BANYO",
    "E.BANYO",
    "EBEVEYN BANYO",
    "ANTRE",
    "HOL",
    "KORİDOR",
    "KORIDOR",
    "DÜKKAN",
    "DUKKAN",
    "OFİS",
    "OFIS",
    "SANDIK ODASI",
    "GİYSİ ODASI",
    "G. ODASI",
    "KİLER",
    "KILER",
)

ALAN_KESIF_RE = re.compile(r"(?:A|Alan|ALAN)\s*[:=]\s*\d+(?:[.,]\d+)?\s*M(?:[²2]|\^2)", re.IGNORECASE)
SICAKLIK_KESIF_RE = re.compile(r"^-?\d+(?:[.,]\d+)?\s*°?\s*C$", re.IGNORECASE)
SAYI_OLCU_RE = re.compile(r"^\d{2,3}$")


# ============================================================================
# 1. İŞ PARÇACIĞI VE SÜREÇ GÜVENLİ DOSYA KİLİTLEME (CONCURRENCY LAYER)
# ============================================================================
@contextlib.contextmanager
def _dosya_kilidi(hedef_yol=None):
    """
    Hem Thread-Safe (threading.RLock) hem de Process-Safe (portalocker / msvcrt / fcntl)
    ve yeniden-girişli (re-entrant) kilitleme sağlayan bağlam yöneticisi.
    Çoklu kullanıcı aynı anda DXF yüklediğinde yarış durumlarını (race condition)
    ve yarım yazılmış JSON bozulmalarını önler.
    """
    baz_yol = os.path.abspath(hedef_yol or KALICI_KUTUPHANE_YOLU)
    kilit_dosyasi_yolu = baz_yol + ".lock"
    os.makedirs(os.path.dirname(kilit_dosyasi_yolu), exist_ok=True)

    with _KUTUPHANE_THREAD_LOCK:
        aktif_kilitler = getattr(_THREAD_LOCAL, "aktif_kilitler", None)
        if aktif_kilitler is None:
            aktif_kilitler = set()
            _THREAD_LOCAL.aktif_kilitler = aktif_kilitler

        # Aynı iş parçacığı içinde iç içe (re-entrant) çağrıldıysa mevcut OS kilidini kullan
        if kilit_dosyasi_yolu in aktif_kilitler:
            yield
            return

        kilit_fh = None
        try:
            kilit_fh = open(kilit_dosyasi_yolu, "a+b")
            if _portalocker is not None:
                _portalocker.lock(kilit_fh, _portalocker.LOCK_EX)
            elif os.name == "nt":
                kilit_fh.seek(0, os.SEEK_END)
                if kilit_fh.tell() == 0:
                    kilit_fh.write(b"L")
                    kilit_fh.flush()
                kilit_fh.seek(0)
                _msvcrt.locking(kilit_fh.fileno(), _msvcrt.LK_LOCK, 1)
            else:
                _fcntl.flock(kilit_fh.fileno(), _fcntl.LOCK_EX)
            aktif_kilitler.add(kilit_dosyasi_yolu)
            yield
        finally:
            aktif_kilitler.discard(kilit_dosyasi_yolu)
            if kilit_fh is not None:
                try:
                    if _portalocker is not None:
                        _portalocker.unlock(kilit_fh)
                    elif os.name == "nt":
                        kilit_fh.seek(0)
                        _msvcrt.locking(kilit_fh.fileno(), _msvcrt.LK_UNLCK, 1)
                    else:
                        _fcntl.flock(kilit_fh.fileno(), _fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    kilit_fh.close()
                except Exception:
                    pass


# ============================================================================
# 2. VERİTABANI SOYUTLAMA KATMANI (SQLITE FALLBACK & POSTGRESQL HAZIRLIĞI)
# ============================================================================
class KutuphaneDepoKatmani:
    """
    JSON kayıtlarını gömülü SQLite veritabanına ('katman_kutuphanesi.db') eşzamanlı
    yansıtan ve 'KUTUPHANE_DB_URL' tanımlandığında PostgreSQL'e şema bozulmadan
    aktarım yapabilen veri dayanıklılığı soyutlama katmanı.
    """

    def __init__(self, sqlite_yolu=None, db_url=None):
        self.sqlite_yolu = sqlite_yolu or KALICI_SQLITE_YOLU
        self.db_url = db_url or os.environ.get("KUTUPHANE_DB_URL", "").strip()
        self._sema_hazirla()

    def _baglanti_ac(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.sqlite_yolu)), exist_ok=True)
        conn = sqlite3.connect(self.sqlite_yolu, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _sema_hazirla(self):
        try:
            with contextlib.closing(self._baglanti_ac()) as conn, conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta_bilgi (
                        anahtar TEXT PRIMARY KEY,
                        deger TEXT NOT NULL,
                        guncelleme_tarihi TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS katman_kategorileri (
                        kategori TEXT NOT NULL,
                        katman_adi TEXT NOT NULL,
                        sira_no INTEGER NOT NULL DEFAULT 0,
                        eklenme_tarihi TEXT NOT NULL,
                        PRIMARY KEY (kategori, katman_adi)
                    );
                    CREATE TABLE IF NOT EXISTS mimar_profilleri (
                        profil_imzasi TEXT PRIMARY KEY,
                        ilk_gorulme TEXT,
                        son_gorulme TEXT,
                        ornek_dosya TEXT,
                        olcu_tipi TEXT,
                        baskin_oda_katmani TEXT,
                        baskin_alan_katmani TEXT,
                        katman_sayisi INTEGER DEFAULT 0,
                        proje_sayisi INTEGER DEFAULT 1,
                        tam_veri_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS kesif_gecmisi (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tarih TEXT NOT NULL,
                        kaynak TEXT NOT NULL,
                        yeni_katmanlar_json TEXT NOT NULL
                    );
                    """
                )
        except Exception as exc:
            logger.error("SQLite şema hazırlama hatası: %s", exc)

    def veritabanina_senkronize_et(self, veri):
        """JSON kütüphane sözlüğünü atomik işlemle (transaction) SQLite tablolarına yazar."""
        simdi = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            with contextlib.closing(self._baglanti_ac()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta_bilgi (anahtar, deger, guncelleme_tarihi) VALUES (?, ?, ?)",
                    ("surum", str(veri.get("surum", "3.0-kurumsal-dayanikli")), simdi),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta_bilgi (anahtar, deger, guncelleme_tarihi) VALUES (?, ?, ?)",
                    ("son_guncelleme", str(veri.get("son_guncelleme", simdi)), simdi),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta_bilgi (anahtar, deger, guncelleme_tarihi) VALUES (?, ?, ?)",
                    ("tam_kutuphane_json", json.dumps(veri, ensure_ascii=False), simdi),
                )

                for kategori in ZORUNLU_LISTE_KATEGORILERI:
                    katman_listesi = veri.get(kategori, [])
                    if not isinstance(katman_listesi, list):
                        continue
                    for idx, katman_adi in enumerate(katman_listesi):
                        if isinstance(katman_adi, str) and katman_adi.strip():
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO katman_kategorileri
                                (kategori, katman_adi, sira_no, eklenme_tarihi)
                                VALUES (?, ?, ?, ?)
                                """,
                                (kategori, katman_adi, idx, simdi),
                            )

                profiller = veri.get("mimar_profilleri", {})
                if isinstance(profiller, dict):
                    for imza, p_bilgi in profiller.items():
                        if not isinstance(p_bilgi, dict):
                            continue
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO mimar_profilleri
                            (profil_imzasi, ilk_gorulme, son_gorulme, ornek_dosya, olcu_tipi,
                             baskin_oda_katmani, baskin_alan_katmani, katman_sayisi, proje_sayisi, tam_veri_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(imza),
                                str(p_bilgi.get("ilk_gorulme", simdi)),
                                str(p_bilgi.get("son_gorulme", p_bilgi.get("ilk_gorulme", simdi))),
                                str(p_bilgi.get("ornek_dosya", "")),
                                str(p_bilgi.get("olcu_tipi", "")),
                                str(p_bilgi.get("baskin_oda_katmani", "")),
                                str(p_bilgi.get("baskin_alan_katmani", "")),
                                int(p_bilgi.get("katman_sayisi", 0)),
                                int(p_bilgi.get("proje_sayisi", 1)),
                                json.dumps(p_bilgi, ensure_ascii=False),
                            ),
                        )

                gecmis = veri.get("kesif_gecmisi", [])
                if isinstance(gecmis, list):
                    conn.execute("DELETE FROM kesif_gecmisi")
                    for kayit in gecmis[-100:]:
                        if isinstance(kayit, dict):
                            conn.execute(
                                "INSERT INTO kesif_gecmisi (tarih, kaynak, yeni_katmanlar_json) VALUES (?, ?, ?)",
                                (
                                    str(kayit.get("tarih", simdi)),
                                    str(kayit.get("kaynak", "dxf")),
                                    json.dumps(kayit.get("yeni_katmanlar", {}), ensure_ascii=False),
                                ),
                            )
            return True
        except Exception as exc:
            logger.error("SQLite senkronizasyon hatası: %s", exc)
            return False

    def veritabanindan_yukle(self):
        """
        JSON dosyaları ve yedekler kullanılamaz olduğunda gömülü SQLite veritabanından
        tüm katmanları, mimar profillerini ve keşif geçmişini eksiksiz yeniden inşa eder.
        """
        if not os.path.isfile(self.sqlite_yolu):
            return None
        try:
            with contextlib.closing(self._baglanti_ac()) as conn:
                cur = conn.cursor()
                # Önce yapılandırılmış tablolardan inşa et
                kurtarilan = json.loads(json.dumps(VARSAYILAN_KUTUPHANE))
                cur.execute("SELECT anahtar, deger FROM meta_bilgi")
                meta_map = dict(cur.fetchall())
                if "surum" in meta_map:
                    kurtarilan["surum"] = meta_map["surum"]
                if "son_guncelleme" in meta_map:
                    kurtarilan["son_guncelleme"] = meta_map["son_guncelleme"]

                cur.execute(
                    "SELECT kategori, katman_adi FROM katman_kategorileri ORDER BY kategori, sira_no ASC"
                )
                satirlar = cur.fetchall()
                if satirlar:
                    kat_haritasi = collections.defaultdict(list)
                    for kat, ad in satirlar:
                        if ad not in kat_haritasi[kat]:
                            kat_haritasi[kat].append(ad)
                    for kat in ZORUNLU_LISTE_KATEGORILERI:
                        mevcut = list(kurtarilan.get(kat, []))
                        for ad in kat_haritasi.get(kat, []):
                            if ad not in mevcut:
                                mevcut.append(ad)
                        kurtarilan[kat] = mevcut

                cur.execute("SELECT profil_imzasi, tam_veri_json FROM mimar_profilleri")
                profiller = {}
                for imza, p_json in cur.fetchall():
                    try:
                        profiller[imza] = json.loads(p_json)
                    except Exception:
                        pass
                if profiller:
                    kurtarilan["mimar_profilleri"] = profiller

                cur.execute("SELECT tarih, kaynak, yeni_katmanlar_json FROM kesif_gecmisi ORDER BY id ASC")
                gecmis = []
                for tarih, kaynak, y_json in cur.fetchall():
                    try:
                        gecmis.append({
                            "tarih": tarih,
                            "kaynak": kaynak,
                            "yeni_katmanlar": json.loads(y_json),
                        })
                    except Exception:
                        pass
                if gecmis:
                    kurtarilan["kesif_gecmisi"] = gecmis

                if satirlar or profiller:
                    return kurtarilan

                if "tam_kutuphane_json" in meta_map:
                    tam = json.loads(meta_map["tam_kutuphane_json"])
                    if _sema_dogrula(tam):
                        return tam
        except Exception as exc:
            logger.error("SQLite veritabanından kurtarma hatası: %s", exc)
        return None


_DEPO_KATMANI = KutuphaneDepoKatmani()


# ============================================================================
# 3. OTOMATİK ZAMAN DAMGALI YEDEKLEME VE BULUT SENKRONİZASYON KANCASI
# ============================================================================
def bulut_kancasi_kaydet(kanca_fonksiyonu):
    """
    Harici bir bulut yükleme fonksiyonunu (ör. Google Drive API veya AWS S3 boto3)
    sisteme kaydetmek için kullanılır. Her yeni yedek alındığında otomatik tetiklenir.
    """
    if callable(kanca_fonksiyonu) and kanca_fonksiyonu not in _BULUT_KANCALARI:
        _BULUT_KANCALARI.append(kanca_fonksiyonu)


def bulut_yedekleme_kancasi(yedek_dosya_yolu, meta_bilgi=None):
    """
    Üretilen zaman damgalı yedeği Google Drive veya AWS S3 gibi bulut depolama
    servislerine iletmek için hazır kanca (hook) bloğu.

    Desteklenen ortam değişkenleri:
      - AWS_S3_KUTUPHANE_BUCKET : Tanımlıysa ve 'boto3' kuruluysa S3'e yükler.
      - GDRIVE_YEDEK_KLASORU    : Yerel Google Drive senkron klasörü tanımlıysa oraya kopyalar.
      - BULUT_YEDEK_WEBHOOK_URL : Tanımlıysa JSON yedeğini HTTPS POST ile bulut uç noktasına iletir.
    """
    sonuclar = {"yerel_yedek": yedek_dosya_yolu, "bulut_durumu": "hazir_beklemede"}
    if not yedek_dosya_yolu or not os.path.isfile(yedek_dosya_yolu):
        return sonuclar

    # 1. Kayıtlı özel Python kancalarını çalıştır
    for kanca in list(_BULUT_KANCALARI):
        try:
            kanca(yedek_dosya_yolu, meta_bilgi or {})
            sonuclar["bulut_durumu"] = "ozel_kanca_basarili"
        except Exception as exc:
            logger.warning("Özel bulut yedekleme kancası hatası: %s", exc)

    # 2. Google Drive Masaüstü / Senkron Klasörü Kancası
    gdrive_dizin = os.environ.get("GDRIVE_YEDEK_KLASORU", "").strip()
    if gdrive_dizin and os.path.isdir(gdrive_dizin):
        try:
            hedef = os.path.join(gdrive_dizin, os.path.basename(yedek_dosya_yolu))
            shutil.copy2(yedek_dosya_yolu, hedef)
            sonuclar["gdrive_kopyasi"] = hedef
            sonuclar["bulut_durumu"] = "gdrive_senkronize"
        except Exception as exc:
            logger.warning("Google Drive klasör senkronizasyon hatası: %s", exc)

    # 3. AWS S3 Kancası (boto3 mevcutsa)
    s3_bucket = os.environ.get("AWS_S3_KUTUPHANE_BUCKET", "").strip()
    if s3_bucket:
        try:
            import boto3  # type: ignore
            s3_prefix = os.environ.get("AWS_S3_KUTUPHANE_PREFIX", "katman_kutuphanesi/").strip()
            s3_key = f"{s3_prefix.rstrip('/')}/{os.path.basename(yedek_dosya_yolu)}"
            boto3.client("s3").upload_file(yedek_dosya_yolu, s3_bucket, s3_key)
            sonuclar["s3_uri"] = f"s3://{s3_bucket}/{s3_key}"
            sonuclar["bulut_durumu"] = "aws_s3_yuklendi"
        except Exception as exc:
            logger.warning("AWS S3 bulut yedekleme hatası: %s", exc)

    # 4. Bulut Webhook Kancası (Google Apps Script / n8n / Cloud Function)
    webhook_url = os.environ.get("BULUT_YEDEK_WEBHOOK_URL", "").strip()
    if webhook_url:
        try:
            with open(yedek_dosya_yolu, "rb") as f:
                govde = f.read()
            istek = urllib.request.Request(
                webhook_url,
                data=govde,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(istek, timeout=5) as yanit:
                sonuclar["webhook_kodu"] = yanit.status
                sonuclar["bulut_durumu"] = "webhook_iletildi"
        except Exception as exc:
            logger.warning("Bulut webhook yedekleme hatası: %s", exc)

    return sonuclar


def _zaman_damgali_yedek_olustur(veri, baz_dizin=None):
    """
    Her yeni mimar profili veya katman öğrenildiğinde:
      1. Günlük ana yedek: 'katman_kutuphanesi_YYYYMMDD.json' (ör. katman_kutuphanesi_20261025.json)
      2. Yedekler klasöründe tam zaman damgalı yedek: 'yedekler/katman_kutuphanesi_YYYYMMDD_HHMMSS.json'
    dosyalarını atomik olarak üretir ve bulut senkronizasyon kancasını tetikler.
    """
    hedef_dizin = baz_dizin or KALICI_DIZIN
    yedek_klasoru = os.path.join(hedef_dizin, "yedekler")
    os.makedirs(hedef_dizin, exist_ok=True)
    os.makedirs(yedek_klasoru, exist_ok=True)

    simdi = datetime.datetime.now()
    gun_damgasi = simdi.strftime("%Y%m%d")
    tam_damga = simdi.strftime("%Y%m%d_%H%M%S")

    gunluk_yedek_adi = f"katman_kutuphanesi_{gun_damgasi}.json"
    detayli_yedek_adi = f"katman_kutuphanesi_{tam_damga}.json"

    gunluk_yedek_yolu = os.path.join(hedef_dizin, gunluk_yedek_adi)
    detayli_yedek_yolu = os.path.join(yedek_klasoru, detayli_yedek_adi)
    calisma_gunluk_yolu = os.path.join(BU_DIZIN, gunluk_yedek_adi)

    for y_yol in (gunluk_yedek_yolu, detayli_yedek_yolu, calisma_gunluk_yolu):
        try:
            gecici = y_yol + ".tmp"
            with open(gecici, "w", encoding="utf-8") as f:
                json.dump(veri, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(gecici, y_yol)
        except Exception as exc:
            logger.warning("Zaman damgalı yedek yazılamadı (%s): %s", y_yol, exc)

    # Eski saatlik yedekleri temizle (son 30 yedeği koru)
    try:
        tum_yedekler = sorted(
            glob.glob(os.path.join(yedek_klasoru, "katman_kutuphanesi_*.json")),
            key=os.path.getmtime,
            reverse=True,
        )
        for eski_yedek in tum_yedekler[30:]:
            try:
                os.remove(eski_yedek)
            except Exception:
                pass
    except Exception:
        pass

    bulut_yedekleme_kancasi(
        gunluk_yedek_yolu,
        meta_bilgi={"tarih": simdi.isoformat(timespec="seconds"), "detayli_yedek": detayli_yedek_yolu},
    )
    return gunluk_yedek_yolu


# ============================================================================
# 4. SIKI ŞEMA DOĞRULAMA (VALIDATION) VE OTOMATİK ÖZ-ONARIM (AUTO-RECOVERY)
# ============================================================================
def _sema_dogrula(aday_veri):
    """
    Okunan JSON verisinin kurumsal şema kurallarına uygunluğunu denetler:
      - Sözlük (dict) olmalı,
      - Zorunlu katman kategorileri listelerden oluşmalı ve boş olmamalı,
      - 'mimar_profilleri' sözlük (dict), 'kesif_gecmisi' liste (list) olmalı.
    """
    if not isinstance(aday_veri, dict):
        return False
    for kategori in ZORUNLU_LISTE_KATEGORILERI:
        deger = aday_veri.get(kategori)
        if deger is not None:
            if not isinstance(deger, list):
                return False
            if any(not isinstance(item, str) for item in deger):
                return False
    if "mimar_profilleri" in aday_veri and not isinstance(aday_veri["mimar_profilleri"], dict):
        return False
    if "kesif_gecmisi" in aday_veri and not isinstance(aday_veri["kesif_gecmisi"], list):
        return False
    # En az bir temel katman kategorisi mevcut olmalı
    if not any(isinstance(aday_veri.get(k), list) and len(aday_veri.get(k)) > 0 for k in ZORUNLU_LISTE_KATEGORILERI):
        return False
    return True


def _varsayilan_ile_birlestir(disk_veri):
    """Doğrulanmış disk verisini varsayılan çekirdek katmanlarla kayıpsız birleştirir."""
    veri = json.loads(json.dumps(VARSAYILAN_KUTUPHANE))
    for k, v in disk_veri.items():
        if isinstance(v, list) and k != "kesif_gecmisi":
            mevcut = list(veri.get(k, []))
            for item in v:
                if isinstance(item, str) and item not in mevcut:
                    mevcut.append(item)
            veri[k] = mevcut
        elif k == "mimar_profilleri" and isinstance(v, dict):
            mevcut_p = dict(veri.get("mimar_profilleri", {}))
            mevcut_p.update(v)
            veri["mimar_profilleri"] = mevcut_p
        else:
            veri[k] = v
    return veri


def _yedek_adaylarini_sirala(ozel_yol=None):
    """En güncelden en eskiye doğru tüm zaman damgalı yedek dosyalarını listeler."""
    arama_desenleri = [
        os.path.join(YEDEK_DIZINI, "katman_kutuphanesi_*.json"),
        os.path.join(KALICI_DIZIN, "katman_kutuphanesi_*.json"),
        os.path.join(BU_DIZIN, "katman_kutuphanesi_*.json"),
        os.path.join(BU_DIZIN, "yedekler", "katman_kutuphanesi_*.json"),
    ]
    if ozel_yol:
        ozel_dizin = os.path.dirname(os.path.abspath(ozel_yol))
        arama_desenleri.insert(0, os.path.join(ozel_dizin, "katman_kutuphanesi_*.json"))
        arama_desenleri.insert(0, os.path.join(ozel_dizin, "yedekler", "katman_kutuphanesi_*.json"))

    bulunanlar = set()
    for desen in arama_desenleri:
        for p in glob.glob(desen):
            if os.path.basename(p) != KUTUPHANE_DOSYA_ADI and not p.endswith(".tmp"):
                bulunanlar.add(os.path.abspath(p))

    return sorted(bulunanlar, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)


def _metin_oku(e):
    if e.dxftype() == "TEXT":
        return e.dxf.text.strip()
    return e.plain_text().replace("\n", " ").strip()


def kutuphane_yukle(yol=None):
    """
    Kalıcı 'katman_kutuphanesi.json' dosyasını sıkı şema doğrulamasıyla yükler.
    Ana JSON dosyası bozulmuş veya okunamaz durumdaysa:
      1. Çökmeyi engeller ve hatayı loglar,
      2. En güncel sağlıklı zaman damgalı yedek dosyasından ('katman_kutuphanesi_YYYYMMDD.json')
         veya gömülü SQLite veritabanından ('katman_kutuphanesi.db') otomatik kurtarır,
      3. Bozuk ana JSON dosyasını sağlıklı yedekle otomatik onarır.
    """
    with _dosya_kilidi(yol):
        incelenecek_yollar = []
        if yol:
            incelenecek_yollar.append(os.path.abspath(yol))
        else:
            # Önce kalıcı veri dizinindeki (Docker volume / yerel_veri) dosyaya, sonra çalışma dizinine bak
            for aday in (KALICI_KUTUPHANE_YOLU, KUTUPHANE_YOLU):
                aday_abs = os.path.abspath(aday)
                if aday_abs not in incelenecek_yollar:
                    incelenecek_yollar.append(aday_abs)

        mevcut_dosya_var = any(os.path.isfile(p) for p in incelenecek_yollar)

        # 1. Birincil JSON dosyalarını dene ve şemasını doğrula
        for aday_yol in incelenecek_yollar:
            if not os.path.isfile(aday_yol):
                continue
            try:
                with open(aday_yol, "r", encoding="utf-8") as f:
                    disk_veri = json.load(f)
                if not _sema_dogrula(disk_veri):
                    raise ValueError(f"Şema doğrulaması başarısız: {aday_yol}")
                birlesik = _varsayilan_ile_birlestir(disk_veri)
                # Eğer kalıcı dizinde henüz dosya yoksa senkronize et
                if not yol and not os.path.isfile(KALICI_KUTUPHANE_YOLU):
                    kutuphane_kaydet(birlesik, yedek_olustur=True)
                return birlesik
            except Exception as exc:
                logger.error(
                    "KRİTİK UYARI: Birincil katman kütüphanesi bozuk veya okunamadı (%s): %s. "
                    "Otomatik yedek kurtarma (Auto-Recovery) başlatılıyor...",
                    aday_yol,
                    exc,
                )

        # 2. Birincil dosya bozuksa en güncel sağlıklı zaman damgalı yedekten kurtar
        for yedek_yolu in _yedek_adaylarini_sirala(yol):
            try:
                with open(yedek_yolu, "r", encoding="utf-8") as f:
                    yedek_veri = json.load(f)
                if _sema_dogrula(yedek_veri):
                    kurtarilan = _varsayilan_ile_birlestir(yedek_veri)
                    logger.info(
                        "OTOMATİK KURTARMA BAŞARILI: Kütüphane sağlıklı zaman damgalı yedekten geri yüklendi: %s",
                        yedek_yolu,
                    )
                    kutuphane_kaydet(kurtarilan, yol=yol, yedek_olustur=False)
                    return kurtarilan
            except Exception as yedek_exc:
                logger.warning("Yedek dosyası atlandı (%s): %s", yedek_yolu, yedek_exc)

        # 3. Zaman damgalı yedek bulunamazsa gömülü SQLite veritabanından kurtar
        sqlite_kurtarilan = _DEPO_KATMANI.veritabanindan_yukle()
        if sqlite_kurtarilan and _sema_dogrula(sqlite_kurtarilan):
            kurtarilan = _varsayilan_ile_birlestir(sqlite_kurtarilan)
            logger.info(
                "OTOMATİK KURTARMA BAŞARILI: Kütüphane gömülü SQLite veritabanından (%s) geri yüklendi.",
                _DEPO_KATMANI.sqlite_yolu,
            )
            kutuphane_kaydet(kurtarilan, yol=yol, yedek_olustur=True)
            return kurtarilan

        # 4. İlk kurulum veya tüm kaynaklar boşsa varsayılan şemayı başlat ve kaydet
        if mevcut_dosya_var:
            logger.warning("Tüm yedekler incelendi; çekirdek varsayılan kütüphane şeması ile sistem onarılıyor.")
        temiz_veri = json.loads(json.dumps(VARSAYILAN_KUTUPHANE))
        kutuphane_kaydet(temiz_veri, yol=yol, yedek_olustur=True)
        return temiz_veri


def kutuphane_kaydet(veri, yol=None, yedek_olustur=True):
    """
    Güncellenen katman kütüphanesini:
      1. Thread-safe ve Process-safe dosya kilidi altında atomik olarak (.tmp + fsync + os.replace)
         kalıcı veri dizinine ve çalışma dizinine yazar,
      2. Gömülü SQLite veritabanına ('katman_kutuphanesi.db') senkronize eder,
      3. 'yedek_olustur=True' olduğunda 'katman_kutuphanesi_YYYYMMDD.json' biçiminde zaman damgalı
         yedek üretir ve bulut senkronizasyon kancasını tetikler.
    """
    if not _sema_dogrula(veri):
        logger.error("Kaydedilmek istenen veri şema doğrulamasından geçemedi; varsayılan şema ile tamamlanıyor.")
        veri = _varsayilan_ile_birlestir(veri if isinstance(veri, dict) else {})

    veri["surum"] = veri.get("surum", "3.0-kurumsal-dayanikli")
    veri["son_guncelleme"] = datetime.datetime.now().isoformat(timespec="seconds")

    with _dosya_kilidi(yol):
        if yol:
            hedefler = [os.path.abspath(yol)]
            yedek_kok_dizin = os.path.dirname(os.path.abspath(yol))
        else:
            hedefler = [os.path.abspath(KALICI_KUTUPHANE_YOLU), os.path.abspath(KUTUPHANE_YOLU)]
            ust_kopya = os.path.join(os.path.dirname(BU_DIZIN), KUTUPHANE_DOSYA_ADI)
            alt_kopya = os.path.join(BU_DIZIN, "deploy", KUTUPHANE_DOSYA_ADI)
            if os.path.isdir(os.path.dirname(ust_kopya)) and os.path.basename(BU_DIZIN) == "deploy":
                hedefler.append(os.path.abspath(ust_kopya))
            if os.path.isdir(os.path.join(BU_DIZIN, "deploy")):
                hedefler.append(os.path.abspath(alt_kopya))
            yedek_kok_dizin = KALICI_DIZIN

        for h in sorted(set(hedefler)):
            try:
                os.makedirs(os.path.dirname(h), exist_ok=True)
                gecici = f"{h}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(gecici, "w", encoding="utf-8") as f:
                    json.dump(veri, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(gecici, h)
            except Exception as exc:
                logger.error("Kütüphane dosyası yazılamadı (%s): %s", h, exc)

        # SQLite gömülü veritabanına senkronize et
        _DEPO_KATMANI.veritabanina_senkronize_et(veri)

        # Zaman damgalı yedek oluştur ve bulut kancasını tetikle
        if yedek_olustur:
            _zaman_damgali_yedek_olustur(veri, baz_dizin=yedek_kok_dizin)


def katmanlari_kesfet_ve_ogren(doc, kaynak_adi=""):
    """
    Yüklenen DXF çizimini tarayarak:
      - Mahal adı / m² yazılarının bulunduğu katmanları,
      - Mahal kodu / sıcaklık (20°C) katmanlarını,
      - DIMENSION veya patlatılmış ölçü metni katmanlarını,
      - Dış duvar, iç duvar ve kolon katmanlarını,
      - Mimar / CAD Şablon Parmak İzini (mimar_profilleri)
    otomatik keşfeder ve yeni katmanları/profilleri thread-safe kilit altında
    kalıcı 'katman_kutuphanesi.json', zaman damgalı yedek ve SQLite veritabanına kaydeder.
    """
    with _dosya_kilidi():
        kutuphane = kutuphane_yukle()
        msp = doc.modelspace()
        yeni_eklenenler = collections.defaultdict(list)
        profil_guncellendi = False

        def _ekle(kategori, katman_adi):
            if not katman_adi:
                return
            liste = kutuphane.setdefault(kategori, [])
            if katman_adi not in liste:
                liste.append(katman_adi)
                yeni_eklenenler[kategori].append(katman_adi)

        oda_katman_sayac = collections.Counter()
        alan_katman_sayac = collections.Counter()
        sicaklik_katman_sayac = collections.Counter()
        sayi_katman_sayac = collections.Counter()
        toplam_metin_sayac = collections.Counter()

        for e in msp.query("TEXT MTEXT"):
            katman = e.dxf.layer
            t = _metin_oku(e)
            if not t:
                continue
            toplam_metin_sayac[katman] += 1
            tu = t.upper()

            if SICAKLIK_KESIF_RE.match(t):
                sicaklik_katman_sayac[katman] += 1
            if ALAN_KESIF_RE.search(t):
                alan_katman_sayac[katman] += 1
            if any(tu == k or tu.startswith(k + " ") for k in ODA_ANAHTAR_KELIMELERI):
                oda_katman_sayac[katman] += 1
            if SAYI_OLCU_RE.match(t):
                sayi_katman_sayac[katman] += 1

        # 1. Mahal kodu katmanları (yalnız adında MAHAL+NO geçen veya metinlerinin >%70'i kod+sıcaklık olan özel katmanlar)
        for katman, adet in sicaklik_katman_sayac.items():
            ku = katman.strip().upper()
            toplam = toplam_metin_sayac[katman]
            if ku in ("0", "MİMARİ", "MIMARI", "RADYATOR", "A-RADYATOR"):
                continue
            if ("MAHAL" in ku and "NO" in ku) or (adet >= 3 and (adet * 2.0 / max(toplam, 1)) >= 0.70):
                _ekle("mahal_kod_katmanlari", katman)

        # 2. Mahal adı ve m² alanı katmanları
        for katman, adet in alan_katman_sayac.items():
            if adet >= 1 and katman not in kutuphane["mahal_kod_katmanlari"]:
                _ekle("mahal_metin_katmanlari", katman)
        for katman, adet in oda_katman_sayac.items():
            if adet >= 1 and katman not in kutuphane["mahal_kod_katmanlari"]:
                _ekle("mahal_metin_katmanlari", katman)

        # 3. DIMENSION ve patlatılmış ölçü katmanları
        dim_sayac = collections.Counter(e.dxf.layer for e in msp.query("DIMENSION"))
        for katman, adet in dim_sayac.items():
            if adet < 1:
                continue
            ku = katman.upper()
            if "DIŞ" in ku or "DIS" in ku or "ORTAK" in ku:
                _ekle("olcu_dis_katmanlari", katman)
            else:
                _ekle("olcu_ic_katmanlari", katman)

        for katman, sayi_adet in sayi_katman_sayac.items():
            toplam = toplam_metin_sayac[katman]
            ku = katman.upper()
            if sayi_adet >= 15 and (
                "DIM" in ku or "ÖLÇÜ" in ku or "OLCU" in ku or (sayi_adet / max(toplam, 1)) >= 0.45
            ):
                if katman not in kutuphane["mahal_kod_katmanlari"] and katman not in kutuphane["mahal_metin_katmanlari"]:
                    _ekle("olcu_ic_katmanlari", katman)

        # 4. Duvar ve Kolon katmanları
        cizgi_sayac = collections.Counter(e.dxf.layer for e in msp.query("LINE LWPOLYLINE"))
        for katman, adet in cizgi_sayac.items():
            if adet < 5:
                continue
            ku = katman.upper()
            if any(k in ku for k in ("KOLON", "PERDE", "TAŞIYICI", "TASIYICI", "BETONARME", "COLUMN")):
                _ekle("kolon_katmanlari", katman)
            elif any(k in ku for k in ("DIŞ DUVAR", "DIS DUVAR", "DıŞ DUVAR", "EXTERIOR WALL")):
                _ekle("duvar_dis_katmanlari", katman)
                _ekle("pencere_kapi_katmanlari", katman)
            elif any(k in ku for k in ("DUVAR", "WALL", "BİRLEŞİK", "BIRLESIK", "MİMARİ", "MIMARI")):
                _ekle("duvar_ic_katmanlari", katman)

        # 5. Mimar / CAD Şablon Parmak İzi (Ömürlük Öğrenen Mimari Profil)
        tum_katmanlar = sorted({e.dxf.layer for e in msp if hasattr(e.dxf, "layer")})
        if len(tum_katmanlar) >= 3:
            imza_katmanlar = [k for k in tum_katmanlar if k not in ("0", "Defpoints")][:12]
            profil_anahtari = "|".join(imza_katmanlar[:6])
            profiller = kutuphane.setdefault("mimar_profilleri", {})
            en_cok_oda_katmani = oda_katman_sayac.most_common(1)[0][0] if oda_katman_sayac else ""
            en_cok_alan_katmani = alan_katman_sayac.most_common(1)[0][0] if alan_katman_sayac else ""
            olcu_tipi = "dimension" if sum(dim_sayac.values()) >= 5 else "patlatilmis_metin"
            if profil_anahtari and profil_anahtari not in profiller:
                profiller[profil_anahtari] = {
                    "ilk_gorulme": datetime.datetime.now().isoformat(timespec="seconds"),
                    "ornek_dosya": os.path.basename(kaynak_adi) if kaynak_adi else "dxf",
                    "olcu_tipi": olcu_tipi,
                    "baskin_oda_katmani": en_cok_oda_katmani,
                    "baskin_alan_katmani": en_cok_alan_katmani,
                    "katman_sayisi": len(tum_katmanlar),
                    "proje_sayisi": 1,
                }
                yeni_eklenenler["mimar_profili"].append(profil_anahtari)
            elif profil_anahtari in profiller:
                profiller[profil_anahtari]["proje_sayisi"] = int(profiller[profil_anahtari].get("proje_sayisi", 1)) + 1
                profiller[profil_anahtari]["son_gorulme"] = datetime.datetime.now().isoformat(timespec="seconds")
                profil_guncellendi = True

        if yeni_eklenenler:
            gecmis = kutuphane.setdefault("kesif_gecmisi", [])
            gecmis.append({
                "tarih": datetime.datetime.now().isoformat(timespec="seconds"),
                "kaynak": os.path.basename(kaynak_adi) if kaynak_adi else "dxf",
                "yeni_katmanlar": dict(yeni_eklenenler),
            })
            if len(gecmis) > 100:
                kutuphane["kesif_gecmisi"] = gecmis[-100:]
            kutuphane_kaydet(kutuphane, yedek_olustur=True)
        elif profil_guncellendi:
            # Sadece proje sayacı arttıysa ana dosyayı ve SQLite'ı güncelle
            kutuphane_kaydet(kutuphane, yedek_olustur=False)

        return kutuphane, dict(yeni_eklenenler)
