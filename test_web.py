import glob, hashlib, io, json, os, re, sqlite3, sys, tempfile, time, zipfile, datetime

COZUM = [d for d in glob.glob(os.path.join(os.environ["USERPROFILE"], "Desktop", "Antigravity", "Projeler", "*"))
         if os.path.isdir(d) and len(os.path.basename(d)) == 7 and os.path.basename(d)[1:3] == "al"][0]
DEPLOY = os.path.join(COZUM, "deploy")
VERI = tempfile.mkdtemp(prefix="web_test_")
os.environ.update(VERI_DIZINI=VERI, ADMIN_EPOSTA="admin@test.local", ADMIN_SIFRE="AdminSifre123", WEB_GIZLI="test-gizli")
sys.path.insert(0, DEPLOY)
from web import app as W   # noqa

c = W.app.test_client()
hatalar = []
def kontrol(ad, ok, ay=""):
    print(("OK    " if ok else "HATA  ") + ad, ay)
    if not ok:
        hatalar.append(ad)

def csrf(cl, url="/giris"):
    html = cl.get(url).get_data(as_text=True)
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    return m.group(1) if m else ""

def post(cl, url, veri=None, tok_url="/giris", **kw):
    veri = dict(veri or {})
    veri.setdefault("csrf", csrf(cl, tok_url))
    return cl.post(url, data=veri, **kw)

def kayit_giris(cl, eposta, sifre="Guclu1234", ad="Test Kullanici"):
    r = post(cl, "/kayit", {"ad": ad, "eposta": eposta, "sifre": sifre}, "/kayit")
    r2 = post(cl, "/giris", {"eposta": eposta, "sifre": sifre})
    return r, r2

def db():
    con = sqlite3.connect(W.DB_YOLU); con.row_factory = sqlite3.Row; return con

def bekle(is_id, sn=240):
    t = time.time()
    while time.time() - t < sn:
        d = db().execute("select durum from is_kaydi where id=?", (is_id,)).fetchone()["durum"]
        if d in ("tamam", "hata"):
            return d
        time.sleep(1)
    return "zaman_asimi"

# 1) giris yapmadan korumali sayfalar
for u in ("/panel", "/yeni", "/admin"):
    r = c.get(u)
    kontrol(f"anonim {u} -> girise yonlenir", r.status_code == 302 and "/giris" in r.headers["Location"])

# 2) CSRF
r = c.post("/giris", data={"eposta": "x@y.zz", "sifre": "abcdefgh"})
kontrol("CSRF belirteci olmadan POST reddedilir (400)", r.status_code == 400, f"status={r.status_code}")

# 3) kayit dogrulamalari
r, _ = kayit_giris(c, "zayif@test.local", sifre="kisa")
kontrol("kisa sifre reddedilir", db().execute("select 1 from kullanici where eposta='zayif@test.local'").fetchone() is None)
c1 = W.app.test_client()
r, r2 = kayit_giris(c1, "musteri@test.local")
kontrol("kayit + giris basarili", r2.status_code == 302 and "/panel" in r2.headers["Location"], f"status={r2.status_code}")
r, _ = kayit_giris(W.app.test_client(), "musteri@test.local")
kontrol("ayni e-posta ikinci kez kaydedilemez", db().execute("select count(*) c from kullanici where eposta='musteri@test.local'").fetchone()["c"] == 1)
kontrol("sifre duz metin saklanmiyor", "Guclu1234" not in db().execute("select sifre from kullanici where eposta='musteri@test.local'").fetchone()["sifre"])

# 4) yanlis sifre + hiz siniri
c2 = W.app.test_client()
for i in range(5):
    post(c2, "/giris", {"eposta": "musteri@test.local", "sifre": "yanlis-sifre"})
r = post(c2, "/giris", {"eposta": "musteri@test.local", "sifre": "Guclu1234"})
kontrol("5 hatali denemeden sonra giris kisitlanir (429)", r.status_code == 429, f"status={r.status_code}")

# 5) paketsiz uye proje yukleyemez
def yukle(cl, dosyalar, **ek):
    veri = {"proje_adi": "Test", "kat_sayisi": "2", "ust_tavan": "CC1", "zemin_taban": "TO1", "doseme": "asmolen", "csrf": csrf(cl, "/yeni")}
    veri.update(ek)
    veri["dosyalar"] = [(io.BytesIO(b), ad) for ad, b in dosyalar]
    return cl.post("/yeni", data=veri, content_type="multipart/form-data")
r = c1.get("/yeni")
kontrol("paketsiz uye /yeni'de panele yonlendirilir", r.status_code == 302 and "/panel" in r.headers["Location"])
n0 = db().execute("select count(*) c from is_kaydi").fetchone()["c"]
r = yukle(c1, [("a.dxf", b"x")])
kontrol("paketsiz uye POST ile de is olusturamaz", db().execute("select count(*) c from is_kaydi").fetchone()["c"] == n0)

# 6) normal uye yonetim sayfasina giremez
kontrol("musteri /admin -> 403", c1.get("/admin").status_code == 403)

# 7) yonetici paket tanimlar
ca = W.app.test_client()
r = post(ca, "/giris", {"eposta": "admin@test.local", "sifre": "AdminSifre123"})
kontrol("yonetici girisi", r.status_code == 302)
kid = db().execute("select id from kullanici where eposta='musteri@test.local'").fetchone()["id"]
kontrol("yonetici paneli aciliyor", ca.get("/admin").status_code == 200)
r = post(ca, f"/admin/kullanici/{kid}", {"paket": "baslangic", "varsayilan": "1", "aktif": "1", "hak": "0"}, "/admin")
u = db().execute("select * from kullanici where id=?", (kid,)).fetchone()
kontrol("paket varsayilanlari uygulandi (15 hak, bitis tarihi)", u["paket"] == "baslangic" and u["hak"] == 15 and u["bitis"], f"hak={u['hak']} bitis={u['bitis']}")

# 8) dosya turu kontrolleri
n0 = db().execute("select count(*) c from is_kaydi").fetchone()["c"]
r = yukle(c1, [("proje.dwg", b"AC1027")])
kontrol("DWG reddedilir", r.status_code == 400 and "DXF olarak" in r.get_data(as_text=True) and db().execute("select count(*) c from is_kaydi").fetchone()["c"] == n0)
r = yukle(c1, [("virus.exe", b"MZ")])
kontrol("DXF disi dosya reddedilir", r.status_code == 400 and db().execute("select count(*) c from is_kaydi").fetchone()["c"] == n0)
r = yukle(c1, [("a.dxf", b"x")], ust_tavan="XYZ")
kontrol("gecersiz bina tanimi reddedilir", r.status_code == 400)

# 9) sahte DXF -> is hata ile biter, hak dusmez
r = yukle(c1, [("../../etc/sahte.dxf", b"DXFDATA")])
kontrol("sahte DXF kabul edilip isleme alinir", r.status_code == 302, f"status={r.status_code}")
is_id = r.headers["Location"].rsplit("/", 1)[-1]
kontrol("   sahte DXF isi 'hata' ile biter", bekle(is_id) == "hata")
kontrol("   hata mesaji MAHAL NO'dan bahsediyor", "MAHAL NO" in (db().execute("select mesaj from is_kaydi where id=?", (is_id,)).fetchone()["mesaj"] or ""))
kontrol("   basarisiz isten hak dusmedi", db().execute("select hak from kullanici where id=?", (kid,)).fetchone()["hak"] == 15)
girdi = os.path.join(W.IS_DIZINI, is_id, "girdi")
kontrol("   dosya adi guvenli kaydedildi (yol gezinme yok)", all(".." not in f and "/" not in f for f in os.listdir(girdi)), str(os.listdir(girdi)))

# 10) gercek proje -> Excel uretilir
kal = [f for f in glob.glob(os.path.join(COZUM, "*.dxf")) if "Kalorifer" in f][0]
veri_dxf = open(kal, "rb").read()
t0 = time.time()
r = yukle(c1, [("Kalorifer.dxf", veri_dxf)], proje_adi="Sumru A/B/C")
kontrol("gercek proje yuklendi", r.status_code == 302, f"status={r.status_code}")
is2 = r.headers["Location"].rsplit("/", 1)[-1]
d = bekle(is2)
kontrol("gercek proje isi 'tamam' ile biter", d == "tamam", f"durum={d} sure={time.time()-t0:.0f}s")
row = db().execute("select * from is_kaydi where id=?", (is2,)).fetchone()
dosyalar = json.loads(row["dosyalar"] or "[]")
kontrol("   3 Excel uretildi", len(dosyalar) == 3, str(dosyalar))
kontrol("   basarida hak 15 -> 14", db().execute("select hak from kullanici where id=?", (kid,)).fetchone()["hak"] == 14)

ref_dir = os.path.join(os.environ["USERPROFILE"], ".gemini", "antigravity", "brain", "7cdc3e78-14c5-4940-a59b-70a60515cf22", "scratch", "api_out")
for i, ad in enumerate(dosyalar):
    resp = c1.get(f"/indir/{is2}/{i}")
    ok = resp.status_code == 200
    if ok:
        z = zipfile.ZipFile(io.BytesIO(resp.get_data()))
        h = hashlib.sha256(z.read("xl/worksheets/sheet2.xml")).hexdigest()
        zr = zipfile.ZipFile(os.path.join(ref_dir, ad))
        ok = h == hashlib.sha256(zr.read("xl/worksheets/sheet2.xml")).hexdigest()
        zr.close()
    resp.close()
    kontrol(f"   indirilen dosya Excel'de dogrulanmis cikti ile ayni: {ad}", ok)

# 11) baska uye baskasinin isine/dosyasina erisemez
c3 = W.app.test_client()
kayit_giris(c3, "baska@test.local")
kontrol("baska uye is sayfasini goremez (404)", c3.get(f"/is/{is2}").status_code == 404)
kontrol("baska uye dosya indiremez (404)", c3.get(f"/indir/{is2}/0").status_code == 404)
kontrol("yonetici is sayfasini gorebilir", ca.get(f"/is/{is2}").status_code == 200)
kontrol("gecersiz dosya sirasi 404", c1.get(f"/indir/{is2}/99").status_code == 404 and c1.get(f"/indir/{is2}/-1").status_code == 404)

# 12) suresi dolan uyelik
dun = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
con = db(); con.execute("update kullanici set bitis=? where id=?", (dun, kid)); con.commit(); con.close()
r = c1.get("/yeni")
kontrol("suresi dolan uye yeni proje yukleyemez", r.status_code == 302)
con = db(); con.execute("update kullanici set bitis=?, hak=0 where id=?", ("2099-01-01", kid)); con.commit(); con.close()
kontrol("hakki biten uye yeni proje yukleyemez", c1.get("/yeni").status_code == 302)

# 13) cikis
r = post(c1, "/cikis", {}, "/panel")
kontrol("cikis sonrasi panel kapali", c1.get("/panel").status_code == 302)

# 14) guvenlik basliklari
r = c.get("/giris")
kontrol("guvenlik basliklari var", r.headers.get("X-Frame-Options") == "DENY" and "default-src 'self'" in r.headers.get("Content-Security-Policy", ""))

# 15) eski is temizligi
con = db(); con.execute("update is_kaydi set durum='tamam' where id=?", (is2,)); con.commit(); con.close()
yol = os.path.join(W.IS_DIZINI, is2)
eski = time.time() - (W.SAKLAMA_GUN + 1) * 86400
os.utime(yol, (eski, eski))
W.eski_isleri_temizle()
kontrol("saklama suresi dolan is dosyalari silinir", not os.path.exists(yol) and db().execute("select durum from is_kaydi where id=?", (is2,)).fetchone()["durum"] == "silindi",
        f"klasor_var={os.path.exists(yol)} durum={db().execute('select durum from is_kaydi where id=?', (is2,)).fetchone()['durum']}")

print("\nSONUC:", "TUM TESTLER GECTI" if not hatalar else f"BASARISIZ ({len(hatalar)}): {hatalar}")
import shutil; shutil.rmtree(VERI, ignore_errors=True)
sys.exit(1 if hatalar else 0)
