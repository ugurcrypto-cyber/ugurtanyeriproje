"""
DXF okuyucu - ADIM 1: Dinamik Katman Kütüphaneli Mahal (oda) okuyucu.

Girdi : Mahal kodlarının yazıldığı "temizlenmiş / kodlanmış" DXF veya mimari DXF.
Çıktı : Her kod için {kod, sicaklik, ad, alan_m2, konum, mesafe} listesi + uyarılar + keşfedilen katmanlar.

Kurallar:
  - Önce 'katman_kutuphanesi.py' üzerinden otomatik katman keşfi çalışır ve yeni katmanlar
    'katman_kutuphanesi.json' dosyasına kalıcı olarak kaydedilir.
  - Mahal kodu  : Keşfedilen 'mahal_kod_katmanlari' ('MAHAL NO' vb.) katmanında MTEXT/TEXT, ör. 'N01', 'Z01', '101'
  - Sıcaklık    : Aynı katmanda kodun hemen altındaki MTEXT/TEXT, ör. '20°C', '24°C'
  - Mahal adı   : Keşfedilen 'mahal_metin_katmanlari' ('MİMARİ', 'Mimari', 'Model Birimi - Mahal' vb.)
                  katmanlarında TEXT/MTEXT, ör. 'SALON'; altında 'A : 20.80 M²' / 'A: 24,0 m2'
"""
import math
import re

import ezdxf

from katman_kutuphanesi import katmanlari_kesfet_ve_ogren

KATMAN_KOD = "MAHAL NO"

KOD_RE = re.compile(r"^(?:[A-Z]{1,2}\d{1,3}[A-Z]?|[1-9]\d{2}[A-Z]?)$")
SICAKLIK_RE = re.compile(r"^(-?\d+(?:[.,]\d+)?)\s*°?\s*C$", re.IGNORECASE)
KOD_SICAKLIK_BIRLESIK_RE = re.compile(r"^([A-Z]{0,2}\d{1,3}[A-Z]?)\s+(-?\d+(?:[.,]\d+)?)\s*°?\s*C$", re.IGNORECASE)
ALAN_RE = re.compile(r"^(?:A|Alan|ALAN)?\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*M(?:[²2]|\^2)$", re.IGNORECASE)
ALAN_IC_RE = re.compile(r"(?:A|Alan|ALAN)\s*[:=]\s*(\d+(?:[.,]\d+)?)\s*M(?:[²2]|\^2)", re.IGNORECASE)

GECERSIZ_ADLAR = {
    "SÜZGEÇ", "ASMA BACA", "SAÇAK İZİ", "DEVAMI", "PARSEL", "DAİRE", "BLOK",
    "ARSA ALANI", "KAT ALANI", "DAİRE GİRİŞİ", "BİNA GİRİŞİ", "DÜKKAN GİRİŞİ",
    "YAN BAHÇE", "ARKA BAHÇE", "ÖN BAHÇE", "ÇEKME MESAFESİ", "(TOPRAK ZEMİN)",
    "OTOPARK İZDÜŞÜMÜ", "YAPI YAKLAŞMA MESAFESİ", "BM", "ÇM", "CM", "M.H.", "M.H",
    "H.T.B", "E.T.B", "IŞIKLIK", "ISIKLIK", "ASANSÖR", "ASANSOR", "LİFT ASANSÖR",
    "LIFT ASANSOR", "MAK. DAİRELİ ASANSÖR", "Y O L", "YOL",
}

ONCELIKLI_ODA_ADLARI = (
    "SALON", "YATAK ODASI", "OTURMA ODASI", "ÇOCUK ODASI", "COCUK ODASI", "ODA",
    "MUTFAK NİŞİ", "MUTFAK NISI", "MUTFAK", "BANYO", "E.BANYOSU", "E.BANYO",
    "EBEVEYN BANYO", "WC", "BAY WC", "BAYAN WC", "DUŞ", "DUS", "ANTRE", "HOL",
    "KORİDOR", "KORIDOR", "SANDIK ODASI", "KİLER", "KILER", "G. ODASI", "GİYSİ ODASI",
    "GİYSİ O.", "VESTİYER", "DÜKKAN", "DUKKAN", "OFİS", "OFIS", "ENERJİ ODASI",
    "HAB. ODASI", "SU DEPOSU", "SIĞINAK", "SIGINAK", "OTOPARK", "KAT HOLÜ", "KAT  HOLÜ",
    "MERDİVEN HOLÜ", "BALKON", "TERAS",
)

# Etiketler arası makul mesafe sınırları (çizim birimi).
# ArchiCAD/Revit çıktılarında oda adı ile 'A: 24,0 m2' arasında 'dv:...' ve 'ds:...' satırları
# silindiğinde veya durduğunda dikey mesafe -45 ile -60 birim olabildiği için ALAN_DY_MIN = -80.0 tutulur.
ALAN_DX_MAX, ALAN_DY_MIN, ALAN_DY_MAX = 75.0, -80.0, 10.0
SICAKLIK_DX_MAX, SICAKLIK_DY_MIN, SICAKLIK_DY_MAX = 35.0, -55.0, 15.0
KOD_AD_MAX_MESAFE = 210.0
KOD_AD_MAX_MESAFE_2 = 340.0   # yalnız boşta kalan kodlar için, düşük güven uyarısıyla


def _metin(e):
    if e.dxftype() == "TEXT":
        return e.dxf.text.strip()
    return e.plain_text().replace("\n", " ").strip()


def _num(s):
    return float(s.replace(",", "."))


def _gecersiz_oda_adi_mi(t):
    """Malzeme/kaplama satırlarını (dv:, ds:), kapı/pencere kodlarını (K1, P1) ve teknik notları eler."""
    s = re.sub(r"\s+", " ", t.strip())
    if not s or len(s) <= 1:
        return True
    su = s.upper()
    if su in GECERSIZ_ADLAR:
        return True
    if su.startswith(("DV:", "DS:", "TV:", "TS:", "ENT.", "EĞİM", "EGIM", "RAMPA", "PARAPET", "H:", "5 CM", "YANGIN DUVARI")):
        return True
    if any(k in su for k in ("GİRİŞ", "GIRIS", "NOLU", "ÖLÇEK", "OLCEK", "KOTU", "DİLATASYON", "DILATASYON", "ESASTIR", "İSKAN", "ISKAN", "YÖNETMEL", "PANEL")):
        return True
    if re.match(r"^[KP]\d+$", su):
        return True
    if re.match(r"^[\d.,+\-xX/()%=mM²2\s]+$", s):
        return True
    return False


def _oncelik_puani(ad):
    au = re.sub(r"\s+", " ", ad.strip().upper())
    if any(au == k or au.startswith(k + " ") for k in ONCELIKLI_ODA_ADLARI):
        return 0.0
    return 50.0


def mahal_oku(dxf_yolu_veya_doc):
    kaynak = dxf_yolu_veya_doc if isinstance(dxf_yolu_veya_doc, str) else ""
    doc = ezdxf.readfile(dxf_yolu_veya_doc) if isinstance(dxf_yolu_veya_doc, str) else dxf_yolu_veya_doc

    # ADIM 0: Dinamik Katman Kütüphanesi ile otomatik keşif ve kalıcı öğrenme
    kutuphane, yeni_katmanlar = katmanlari_kesfet_ve_ogren(doc, kaynak_adi=kaynak)
    kod_katman_set = {k.strip().upper() for k in kutuphane.get("mahal_kod_katmanlari", [KATMAN_KOD])}
    mimari_katman_set = {k.strip().upper() for k in kutuphane.get("mahal_metin_katmanlari", [])}

    msp = doc.modelspace()
    uyarilar = []

    kodlar, sicakliklar = [], []
    for e in msp.query("MTEXT TEXT"):
        if e.dxf.layer.strip().upper() not in kod_katman_set:
            continue
        t = _metin(e)
        p = e.dxf.insert
        mb = KOD_SICAKLIK_BIRLESIK_RE.match(t)
        if mb:
            kodlar.append((mb.group(1).upper(), p.x, p.y))
            sicakliklar.append((p.x, p.y - 15.0, _num(mb.group(2))))
            continue
        m = SICAKLIK_RE.match(t)
        if m:
            sicakliklar.append((p.x, p.y, _num(m.group(1))))
        elif KOD_RE.match(t):
            kodlar.append((t.upper(), p.x, p.y))
        else:
            uyarilar.append(f"'{KATMAN_KOD}' katmanında tanınmayan metin: {t!r} ({p.x:.0f},{p.y:.0f})")

    adlar, alanlar = [], []
    oda_etiketleri = []
    yasak_katman_kelimeleri = ("DIMENSION", "ÖLÇÜ", "OLCU", "AKS", "GRID", "YAPISAL", "TAŞIYICI", "TASIYICI")
    for e in msp.query("TEXT MTEXT"):
        katman = e.dxf.layer
        katman_u = katman.strip().upper()
        if katman_u in kod_katman_set or any(k in katman_u for k in yasak_katman_kelimeleri):
            continue
        t = _metin(e)
        p = e.dxf.insert
        m = ALAN_RE.match(t)
        if m:
            alanlar.append((p.x, p.y, _num(m.group(1))))
        elif e.dxftype() == "MTEXT" and ALAN_IC_RE.search(t):
            m_ic = ALAN_IC_RE.search(t)
            satirlar = [s.strip() for s in e.plain_text().splitlines() if s.strip()]
            ilk = satirlar[0] if satirlar else t.split()[0]
            if not ALAN_IC_RE.search(ilk) and not _gecersiz_oda_adi_mi(ilk):
                oda_etiketleri.append({"ad": ilk, "alan": _num(m_ic.group(1)), "x": p.x, "y": p.y})
        elif katman_u in mimari_katman_set or "MAHAL" in katman_u or "YAZI" in katman_u or "MIMARI" in katman_u or "MİMARİ" in katman_u:
            if not _gecersiz_oda_adi_mi(t):
                adlar.append((t, p.x, p.y))

    # ad + alan eşle (alan, adın hemen altında durur; öncelik gerçek oda adlarındadır)
    kullanilan_alan = set()
    adlar_sirali = sorted(adlar, key=lambda item: _oncelik_puani(item[0]))
    for ad, ax, ay in adlar_sirali:
        best = None
        for i, (bx, by, a) in enumerate(alanlar):
            dx, dy = bx - ax, by - ay
            if abs(dx) <= ALAN_DX_MAX and ALAN_DY_MIN <= dy <= ALAN_DY_MAX and i not in kullanilan_alan:
                d = math.hypot(dx, dy)
                if best is None or d < best[0]:
                    best = (d, i, a)
        if best:
            kullanilan_alan.add(best[1])
            oda_etiketleri.append({"ad": ad, "alan": best[2], "x": ax, "y": ay})

    # kod -> oda etiketi: BİREBİR eşleştirme, en yakın çiftten başlayarak.
    adaylar = []
    for ki, (kod, kx, ky) in enumerate(kodlar):
        for oi, oe in enumerate(oda_etiketleri):
            d = math.hypot(oe["x"] - kx, oe["y"] - ky)
            if d <= KOD_AD_MAX_MESAFE:
                adaylar.append((d + _oncelik_puani(oe["ad"]), d, ki, oi))
    adaylar.sort()
    kod_oda, kullanilan_oda = {}, set()
    for _, d, ki, oi in adaylar:
        if ki in kod_oda or oi in kullanilan_oda:
            continue
        kod_oda[ki] = (d, oi)
        kullanilan_oda.add(oi)

    # 2. tur: eşleşmeden kalan kodlar için daha uzak ama BOŞTA kalan etiketler.
    dusuk_guven = set()
    adaylar2 = []
    for ki, (kod, kx, ky) in enumerate(kodlar):
        if ki in kod_oda:
            continue
        for oi, oe in enumerate(oda_etiketleri):
            if oi in kullanilan_oda:
                continue
            d = math.hypot(oe["x"] - kx, oe["y"] - ky)
            if d <= KOD_AD_MAX_MESAFE_2:
                adaylar2.append((d + _oncelik_puani(oe["ad"]), d, ki, oi))
    adaylar2.sort()
    for _, d, ki, oi in adaylar2:
        if ki in kod_oda or oi in kullanilan_oda:
            continue
        kod_oda[ki] = (d, oi)
        kullanilan_oda.add(oi)
        dusuk_guven.add(ki)

    sonuc = []
    for ki, (kod, kx, ky) in enumerate(kodlar):
        sic = None
        for sx, sy, v in sicakliklar:
            dx, dy = sx - kx, sy - ky
            if abs(dx) <= SICAKLIK_DX_MAX and SICAKLIK_DY_MIN <= dy <= SICAKLIK_DY_MAX:
                sic = v
                break
        if sic is None:
            uyarilar.append(f"{kod}: sıcaklık etiketi bulunamadı")

        ad = alan = mesafe = None
        if ki in kod_oda:
            mesafe, oi = kod_oda[ki]
            ad, alan = oda_etiketleri[oi]["ad"], round(float(oda_etiketleri[oi]["alan"]), 2)
            if ki in dusuk_guven:
                uyarilar.append(f"{kod}: DÜŞÜK GÜVEN eşleşme -> {ad} {alan} m² (etiket {mesafe:.0f} birim uzakta, lütfen kontrol edin)")
        else:
            uyarilar.append(f"{kod}: {KOD_AD_MAX_MESAFE:.0f} birim içinde boş mahal adı/alanı bulunamadı")
        if sic is None and ad:
            au = ad.strip().upper()
            if any(b in au for b in ("BANYO", "DUŞ", "DUS")):
                sic = 24.0
        sonuc.append({"kod": kod, "sicaklik": sic, "ad": ad, "alan_m2": alan,
                      "x": kx, "y": ky, "mesafe": mesafe})

    kod_sayisi = {}
    for r in sonuc:
        kod_sayisi[r["kod"]] = kod_sayisi.get(r["kod"], 0) + 1
    for k, n in kod_sayisi.items():
        if n > 1:
            uyarilar.append(f"{k}: çizimde {n} kez geçiyor (çift kod)")

    return {
        "mahaller": sonuc,
        "uyarilar": uyarilar,
        "yeni_katmanlar": yeni_katmanlar,
        "istatistik": {
            "kod": len(kodlar),
            "sicaklik": len(sicakliklar),
            "oda_etiketi": len(oda_etiketleri),
        },
    }
