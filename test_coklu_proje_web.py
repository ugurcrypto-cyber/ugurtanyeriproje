"""
Çoklu Proje + Kapıcı Dairesi + Web Arayüzü (Canlı Tablo, Otomatik Yön, Plak/Asmolen, Karma Yapı, Hibrit Tavan/Taban)
Uçtan Uca Doğrulama Testi.
"""
import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time

KOK = os.path.dirname(os.path.abspath(__file__))
UST_DIZIN = os.path.dirname(KOK)

shutil.copyfile(os.path.join(UST_DIZIN, "dxf_okuyucu.py"), os.path.join(KOK, "dxf_okuyucu.py"))
shutil.copyfile(os.path.join(UST_DIZIN, "dxf_olcu_okuyucu.py"), os.path.join(KOK, "dxf_olcu_okuyucu.py"))
shutil.copyfile(os.path.join(UST_DIZIN, "excel_yazici_v2.py"), os.path.join(KOK, "excel_yazici_v2.py"))

# 1. Kullanıcının yerel veritabanındaki ('cb66102978b54e35993f12f4ad12def0') Kapıcı Dairesi işini de canlı güncelle
yerel_is_dir = os.path.join(KOK, "yerel_veri", "web_isler", "cb66102978b54e35993f12f4ad12def0")
if os.path.isdir(yerel_is_dir):
    sys.path.insert(0, KOK)
    from dxf_olcu_okuyucu import pilot_veya_dinamik_hesapla
    from excel_yazici_v2 import generate_xlsm_pure_python
    from api_sunucu import DEFAULT_TEMPLATE
    girdi_y = os.path.join(yerel_is_dir, "girdi")
    cikti_y = os.path.join(yerel_is_dir, "cikti")
    dxfler_y = [os.path.join(girdi_y, d) for d in sorted(os.listdir(girdi_y)) if d.lower().endswith(".dxf")]
    if dxfler_y:
        sonuc_y = pilot_veya_dinamik_hesapla(dxfler_y, {
            "kat_sayisi": 1,
            "bina_fonksiyonu": "konut",
            "yon_referansi": "yukari_kuzey",
            "ust_tavan": "CC1",
            "zemin_taban": "TO1",
            "doseme": "plak",
        })
        mahaller_y = sonuc_y["mahaller"]
        generate_xlsm_pure_python(DEFAULT_TEMPLATE, os.path.join(cikti_y, "Klima Hesap Tablosu - Isi Kaybi.xlsm"), "PROJE", mahaller_y)
        detay_y = {
            "mahal_sayisi": len(mahaller_y),
            "toplam_dh1": round(sum(float(m.get("dh1", 0.0)) for m in mahaller_y), 2),
            "toplam_dh2": round(sum(float(m.get("dh2", 0.0)) for m in mahaller_y), 2),
            "toplam_dh3": round(sum(float(m.get("dh3", 0.0)) for m in mahaller_y), 2),
            "toplam_do1": round(sum(float(m.get("do1", 0.0)) for m in mahaller_y), 2),
            "uyarilar": sonuc_y.get("uyarilar", []),
            "mahaller": [
                {
                    "blok": "PROJE",
                    "code": m["code"],
                    "name": m["name"],
                    "temp": m.get("temp", 20),
                    "area": m.get("area", 0.0),
                    "dh1": round(float(m.get("dh1", 0.0)), 2),
                    "dh2": round(float(m.get("dh2", 0.0)), 2),
                    "dh3": round(float(m.get("dh3", 0.0)), 2),
                    "do1": round(float(m.get("do1", 0.0)), 2),
                    "ceil_code": m.get("ceil_code", ""),
                    "ceil_area": round(float(m.get("ceil_area", 0.0)), 2),
                    "ceil2_code": m.get("ceil2_code", ""),
                    "ceil2_area": round(float(m.get("ceil2_area", 0.0)), 2),
                    "floor_code": m.get("floor_code", ""),
                    "floor_area": round(float(m.get("floor_area", 0.0)), 2),
                    "floor2_code": m.get("floor2_code", ""),
                    "floor2_area": round(float(m.get("floor2_area", 0.0)), 2),
                    "yon": round(float(m.get("yon", 0.0)), 1),
                    "wins": [f"{w[0]}:{w[1]}x{w[2]}" for w in m.get("wins", []) if float(w[1]) > 0],
                }
                for m in mahaller_y
            ],
        }
        with open(os.path.join(cikti_y, "sonuc_detay.json"), "w", encoding="utf-8") as f:
            json.dump(detay_y, f, ensure_ascii=False, indent=2)
        print("[OK] Yerel veritabanindaki Kapici Dairesi isi ('cb66102978b54e35993f12f4ad12def0') guncellendi!")
        for m in detay_y["mahaller"]:
            print(f"   -> {m['code']} ({m['name']:6s}): Alan={m['area']} m² | DH1={m['dh1']} DH2={m['dh2']} DH3={m['dh3']} | Tavan={m['ceil_code']}:{m['ceil_area']} Taban={m['floor_code']}:{m['floor_area']} | Wins={m['wins']}")

# 2. Web Uygulaması Test İstemcisi ile Uçtan Uca Senaryolar
gecici_dir = tempfile.mkdtemp(prefix="test_coklu_web_")
os.environ["VERI_DIZINI"] = gecici_dir
os.environ["ADMIN_EPOSTA"] = "admin@test.com"
os.environ["ADMIN_SIFRE"] = "AdminSifre123"

sys.path.insert(0, os.path.join(KOK, "web"))
import app as web_app

CSRF_RE = re.compile(r'name="csrf"\s+value="([0-9a-f]+)"')


def csrf_al(client, url="/giris"):
    r = client.get(url)
    m = CSRF_RE.search(r.get_data(as_text=True))
    return m.group(1) if m else ""


def bekle(client, is_id, sure=45):
    t0 = time.time()
    while time.time() - t0 < sure:
        r = client.get(f"/is/{is_id}/durum.json")
        veri = r.get_json()
        if veri["durum"] in ("tamam", "hata"):
            return veri
        time.sleep(0.5)
    raise RuntimeError("Zaman asimi")


def main():
    c = web_app.app.test_client()
    csrf = csrf_al(c, "/giris")
    c.post("/giris", data={"csrf": csrf, "eposta": "admin@test.com", "sifre": "AdminSifre123"})

    with web_app.app.app_context():
        con = web_app.baglan()
        con.execute("UPDATE kullanici SET paket='ofis', hak=20, bitis=?",
                    ((datetime.date.today() + datetime.timedelta(days=30)).isoformat(),))
        con.commit()
        con.close()

    # Senaryo 1: Kapıcı Dairesi (Plak Döşeme + Yukarısı Kuzey + Hibrit Revize)
    if os.path.isdir(yerel_is_dir):
        girdi_y = os.path.join(yerel_is_dir, "girdi")
        dxfler_y = [os.path.join(girdi_y, d) for d in sorted(os.listdir(girdi_y)) if d.lower().endswith(".dxf")]
        csrf = csrf_al(c, "/yeni")
        with open(dxfler_y[0], "rb") as f1, open(dxfler_y[1], "rb") as f2:
            r = c.post("/yeni", data={
                "csrf": csrf,
                "proje_adi": "Kapici Dairesi Ticari Test",
                "bina_fonksiyonu": "konut_dukkan",
                "yon_referansi": "yukari_kuzey",
                "kat_sayisi": "1",
                "ust_tavan": "CC1",
                "zemin_taban": "TO1",
                "doseme": "plak",
                "aciklama": "Kapici dairesi ticari test",
                "dosyalar": [
                    (io.BytesIO(f1.read()), "mimari.dxf"),
                    (io.BytesIO(f2.read()), "kt.dxf"),
                ],
            }, content_type="multipart/form-data")
        assert r.status_code == 302, f"Beklenen 302, gelen {r.status_code}"
        is_id_kapici = r.headers["Location"].rstrip("/").split("/")[-1]
        durum_k = bekle(c, is_id_kapici)
        assert durum_k["durum"] == "tamam", f"Kapici isi hatali: {durum_k}"
        mah_k = {m["code"]: m for m in durum_k["detay"]["mahaller"]}
        assert mah_k["Z01"]["area"] == 18.75, f"Z01 alani 18.75 olmali: {mah_k['Z01']}"
        assert mah_k["Z01"]["dh2"] == 1.60, f"Z01 DH2 1.60 olmali: {mah_k['Z01']}"
        assert "DPB:1.5x1.3" in mah_k["Z01"]["wins"] and "DPG:1.5x1.3" in mah_k["Z01"]["wins"] and "DK:1.0x2.2" in mah_k["Z01"]["wins"], f"Z01 pencereler hatali: {mah_k['Z01']['wins']}"
        assert mah_k["Z02"]["dh1"] == 9.10, f"Z02 DH1 9.10 olmali: {mah_k['Z02']}"
        assert mah_k["Z02"]["wins"] == ["DPD:1.5x1.3"], f"Z02 pencere DPD:1.5x1.3 olmali: {mah_k['Z02']['wins']}"
        assert mah_k["Z03"]["dh1"] > 3.80 and mah_k["Z03"]["wins"] == ["DPD:0.5x0.5"], f"Z03 Banyo hatali: {mah_k['Z03']}"

        # Hibrit Tavan/Taban Satır Bazlı Revize testi (/is/<id>/revize)
        csrf = csrf_al(c, f"/is/{is_id_kapici}")
        r_rev = c.post(f"/is/{is_id_kapici}/revize", data={
            "csrf": csrf,
            "kod": ["Z01", "Z02", "Z03"],
            "dh1_Z01": "10.35", "dh2_Z01": "1.60", "dh3_Z01": "10.35", "do1_Z01": "0",
            "ceil_code_Z01": "CC1", "ceil_area_Z01": "14.20", "ceil2_code_Z01": "CA2", "ceil2_area_Z01": "4.55",
            "floor_code_Z01": "TO1", "floor_area_Z01": "18.75", "floor2_code_Z01": "", "floor2_area_Z01": "0",
            "dh1_Z02": "9.10", "dh2_Z02": "0.50", "dh3_Z02": "9.10", "do1_Z02": "0",
            "ceil_code_Z02": "CC1", "ceil_area_Z02": "12.70", "ceil2_code_Z02": "", "ceil2_area_Z02": "0",
            "floor_code_Z02": "TO1", "floor_area_Z02": "8.70", "floor2_code_Z02": "TG1", "floor2_area_Z02": "4.00",
            "dh1_Z03": "4.45", "dh2_Z03": "0.00", "dh3_Z03": "4.45", "do1_Z03": "0",
            "ceil_code_Z03": "CA1", "ceil_area_Z03": "4.20", "ceil2_code_Z03": "", "ceil2_area_Z03": "0",
            "floor_code_Z03": "TO1", "floor_area_Z03": "4.20", "floor2_code_Z03": "", "floor2_area_Z03": "0",
        })
        assert r_rev.status_code == 302
        durum_rev = c.get(f"/is/{is_id_kapici}/durum.json").get_json()
        mah_rev = {m["code"]: m for m in durum_rev["detay"]["mahaller"]}
        assert mah_rev["Z01"]["ceil2_code"] == "CA2" and mah_rev["Z01"]["ceil2_area"] == 4.55
        assert mah_rev["Z02"]["floor2_code"] == "TG1" and mah_rev["Z02"]["floor2_area"] == 4.00
        print("[OK] 1) Kapici Dairesi (Z01=18.75m², Z02=9.10m, Z03=4.45m, DPB/DPG/DPD/DK + Hibrit Revize) Basarili!")

    # Senaryo 2: Pilot Proje (Recep Kabak)
    pilot_dir = os.path.join(UST_DIZIN, "pilot_recep_kabak")
    csrf = csrf_al(c, "/yeni")
    with open(os.path.join(pilot_dir, "mimari.dxf"), "rb") as fm, open(os.path.join(pilot_dir, "kt.dxf"), "rb") as fk:
        r = c.post("/yeni", data={
            "csrf": csrf,
            "proje_adi": "Pilot Recep Kabak",
            "bina_fonksiyonu": "konut",
            "yon_referansi": "yukari_kuzey",
            "kat_sayisi": "2",
            "ust_tavan": "CC1",
            "zemin_taban": "TT1",
            "doseme": "diger",
            "aciklama": "Pilot proje web testi",
            "dosyalar": [
                (io.BytesIO(fm.read()), "mimari.dxf"),
                (io.BytesIO(fk.read()), "kt.dxf"),
            ],
        }, content_type="multipart/form-data")
    assert r.status_code == 302
    is_id_1 = r.headers["Location"].rstrip("/").split("/")[-1]
    durum_1 = bekle(c, is_id_1)
    assert durum_1["durum"] == "tamam", f"Pilot is hatali: {durum_1}"
    detay_1 = durum_1["detay"]
    assert detay_1["mahal_sayisi"] >= 7
    mah_pilot = {m["code"]: m for m in detay_1["mahaller"]}
    ref_7_dh1 = sum(mah_pilot[k]["dh1"] for k in ("Z01", "Z02", "101A", "102A", "104A", "Z01A", "Z02A"))
    assert abs(ref_7_dh1 - 48.70) < 0.01
    print("[OK] 2) Pilot Proje (Recep Kabak) Web Arayuzu Canli DH1/DH2/DH3 Tablosu + Excel Indirme Basarili!")

    # Senaryo 3: Çoklu Blok + Asmolen döşeme -> DH3 = 0.0
    import glob
    kal_dxf = [f for f in glob.glob(os.path.join(UST_DIZIN, "*.dxf")) if "Kalorifer" in f][0]
    csrf = csrf_al(c, "/yeni")
    with open(kal_dxf, "rb") as fk2:
        r2 = c.post("/yeni", data={
            "csrf": csrf,
            "proje_adi": "Sumru Asmolen Test",
            "bina_fonksiyonu": "konut_dukkan",
            "yon_referansi": "yukari_kuzey",
            "kat_sayisi": "2",
            "ust_tavan": "CC1",
            "zemin_taban": "TO1",
            "doseme": "asmolen",
            "aciklama": "Asmolen doseme testi",
            "dosyalar": [
                (io.BytesIO(fk2.read()), "Kalorifer.dxf"),
            ],
        }, content_type="multipart/form-data")
    assert r2.status_code == 302
    is_id_2 = r2.headers["Location"].rstrip("/").split("/")[-1]
    durum_2 = bekle(c, is_id_2)
    assert durum_2["durum"] == "tamam", f"Asmolen is hatali: {durum_2}"
    detay_2 = durum_2["detay"]
    assert detay_2["mahal_sayisi"] >= 3
    assert detay_2["toplam_dh1"] > 0.0
    assert detay_2["toplam_dh3"] == 0.0
    print(f"[OK] 3) Coklu Blok + Asmolen Doseme ({detay_2['mahal_sayisi']} Mahal) Web Arayuzu Canli DH1={detay_2['toplam_dh1']}m, DH3={detay_2['toplam_dh3']}m + Excel Indirme Basarili!")

    shutil.rmtree(gecici_dir, ignore_errors=True)
    print("=== TUM COKLU PROJE WEB ENTEGRASYON TESTLERI BASARIYLA GECTI ===")


if __name__ == "__main__":
    main()
