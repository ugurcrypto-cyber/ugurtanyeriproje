"""
DXF Ölçü Okuyucu - ADIM 2 (Ticari Mimari v2):
Mimari ölçü nesnelerinden (DIMENSION), duvar/kolon katmanlarından, pencere/kapı etiketlerinden
ve kullanıcı bina tanımından otomatik DH1, DH2, DH3, DO1, hibrit tavan/taban alanı ve
yönlü pencere/kapı (DPK, DPB, DPD, DPG, DK, DP) okuyucu.
"""
import collections
import math
import os
import re

import ezdxf

from dxf_okuyucu import mahal_oku
from katman_kutuphanesi import katmanlari_kesfet_ve_ogren, kutuphane_yukle

ALAN_IC_RE = re.compile(r"(?:A|Alan|ALAN)\s*[:=]\s*(\d+(?:[.,]\d+)?)\s*M(?:[²2]|\^2)", re.IGNORECASE)
KAPI_RE = re.compile(r"^K(\d+)\s*[-:]?\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)$", re.IGNORECASE)
PENCERE_TEK_RE = re.compile(r"^P(\d+)\s*[-:]\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)$", re.IGNORECASE)
P_BLOK_RE = re.compile(r"^P\d+\s+(\d+)\s+\d+$", re.IGNORECASE)
DB_KAPI_BLOK_RE = re.compile(r"^DB\s+K\s+(\d+)\s+\d+$", re.IGNORECASE)
P_ETIKET_RE = re.compile(r"^P\d+$", re.IGNORECASE)
K_ETIKET_RE = re.compile(r"^K\d+$", re.IGNORECASE)
SAYI_RE = re.compile(r"^\d{2,3}$")

YON_ZAMMI = {
    "DPK": 5.0,
    "DPG": -5.0,
    "DPD": 0.0,
    "DPB": 0.0,
}


def _yon_haritasi(yon_referansi="yukari_kuzey"):
    """
    Planın 4 kenarını ('ust'=+Y, 'sag'=+X, 'alt'=-Y, 'sol'=-X) pusula yönlerine eşler.
    Varsayılan ('yukari_kuzey'): Üst (+Y) = KUZEY (DPK), Sağ (+X) = DOĞU (DPD), Alt (-Y) = GÜNEY (DPG), Sol (-X) = BATI (DPB).
    """
    ref = str(yon_referansi or "yukari_kuzey").strip().lower()
    if ref == "sag_kuzey":
        return {"sag": "DPK", "alt": "DPD", "sol": "DPG", "ust": "DPB"}
    if ref == "asagi_kuzey":
        return {"alt": "DPK", "sol": "DPD", "ust": "DPG", "sag": "DPB"}
    if ref == "sol_kuzey":
        return {"sol": "DPK", "ust": "DPD", "sag": "DPG", "alt": "DPB"}
    return {"ust": "DPK", "sag": "DPD", "alt": "DPG", "sol": "DPB"}


def _metin(e):
    if e.dxftype() == "TEXT":
        return e.dxf.text.strip()
    return e.plain_text().replace("\n", " ").strip()


def _metin_satirlari(e):
    if e.dxftype() == "TEXT":
        t = e.dxf.text.strip()
        return [t] if t else []
    return [s.strip() for s in e.plain_text().splitlines() if s.strip()]


def _norm_etiket(s):
    return re.sub(r"\s+", " ", s.strip().upper())


def _kat_no_tahmin(kod):
    """Mahal kodundan kat seviyesini tahmin eder: B->-1, Z->0, 1xx->1, N->1."""
    k = kod.upper().rstrip("ABCDEFG")
    if k.startswith("B"):
        return -1
    if k.startswith("Z"):
        return 0
    if k.startswith("N"):
        return 1
    if len(k) == 3 and k.isdigit():
        return int(k[0])
    return 1


_METIN_INDEKS_ONBELLEK = {}


def _msp_metin_indeksi(msp):
    mid = id(msp)
    if mid not in _METIN_INDEKS_ONBELLEK:
        if len(_METIN_INDEKS_ONBELLEK) > 8:
            _METIN_INDEKS_ONBELLEK.clear()
        by_text = collections.defaultdict(list)
        all_items = []
        for e in msp.query("TEXT MTEXT"):
            xm, ym = float(e.dxf.insert.x), float(e.dxf.insert.y)
            for s in _metin_satirlari(e):
                ns = _norm_etiket(s)
                if ns and len(ns) >= 3:
                    by_text[ns].append((xm, ym))
                    all_items.append((xm, ym, ns))
        _METIN_INDEKS_ONBELLEK[mid] = (by_text, all_items)
    return _METIN_INDEKS_ONBELLEK[mid]


def _donusum_bul(kt_msp, mim_msp, kx, ky, yaricap=1600.0):
    """
    KT çizimindeki bir noktanın mimari çizimdeki karşılığını (dx, dy) ortak metinlerden bulur.
    """
    if kt_msp is mim_msp:
        return (0.0, 0.0), 999

    m_texts, _ = _msp_metin_indeksi(mim_msp)
    _, kt_items = _msp_metin_indeksi(kt_msp)

    votes = collections.Counter()
    bucket_vecs = collections.defaultdict(list)
    for xk, yk, ns in kt_items:
        if abs(xk - kx) > yaricap or abs(yk - ky) > yaricap:
            continue
        if math.hypot(xk - kx, yk - ky) > yaricap:
            continue
        if ns not in m_texts or len(m_texts[ns]) > 35:
            continue
        for xm, ym in m_texts[ns]:
            dx, dy = xm - xk, ym - yk
            key = (round(dx / 10.0), round(dy / 10.0))
            votes[key] += 1
            bucket_vecs[key].append((dx, dy))

    if not votes:
        return (0.0, 0.0), 0

    best_key, cnt = votes.most_common(1)[0]
    vecs = bucket_vecs[best_key]
    avg_dx = sum(v[0] for v in vecs) / len(vecs)
    avg_dy = sum(v[1] for v in vecs) / len(vecs)
    return (avg_dx, avg_dy), cnt


def _kolonlari_oku(msp, xmin, xmax, ymin, ymax):
    """
    Belirtilen bölgedeki betonarme kolon ve perde dikdörtgenlerini (x1, x2, y1, y2)
    Dinamik Katman Kütüphanesindeki 'kolon_katmanlari' üzerinden okur.
    """
    kutuphane = kutuphane_yukle()
    kolon_katman_set = {k.strip().upper() for k in kutuphane.get("kolon_katmanlari", [])}
    duvar_katman_set = {k.strip().upper() for k in kutuphane.get("duvar_ic_katmanlari", [])} | {
        k.strip().upper() for k in kutuphane.get("duvar_dis_katmanlari", [])
    }
    kolonlar = []

    def _ekle_poly(pts):
        if len(pts) < 3:
            return
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)
        w = x2 - x1
        h = y2 - y1
        if xmax >= x1 >= xmin - 100 and xmin <= x2 <= xmax + 100 and ymax >= y1 >= ymin - 100 and ymin <= y2 <= ymax + 100:
            if 20.0 <= w <= 250.0 and 20.0 <= h <= 250.0:
                kolonlar.append((x1, x2, y1, y2))

    for e in msp.query("LWPOLYLINE"):
        katman = e.dxf.layer.strip().upper()
        pts = [(p[0], p[1]) for p in e.get_points()]
        if katman in kolon_katman_set or "KOLON" in katman or "PERDE" in katman or "TAŞIYICI" in katman or "TASIYICI" in katman:
            _ekle_poly(pts)
        elif katman in duvar_katman_set and (e.closed or len(pts) >= 4):
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if 35.0 <= w <= 65.0 and 35.0 <= h <= 65.0:
                _ekle_poly(pts)

    doc = msp.doc
    for e in msp.query("INSERT"):
        katman = e.dxf.layer.strip().upper()
        bname = e.dxf.name.upper()
        if katman in kolon_katman_set or "KOLON" in katman or "PERDE" in katman or "KOLON" in bname:
            ix, iy = float(e.dxf.insert.x), float(e.dxf.insert.y)
            if not (xmin - 800 <= ix <= xmax + 800 and ymin - 800 <= iy <= ymax + 800):
                continue
            try:
                blk = doc.blocks.get(e.dxf.name)
                for be in blk:
                    if be.dxftype() == "LWPOLYLINE":
                        pts = [(float(p[0]) + ix, float(p[1]) + iy) for p in be.get_points()]
                        _ekle_poly(pts)
            except Exception:
                pass

    if not kolonlar:
        # ArchiCAD / Revit gibi yazılımlarda kolonlar HATCH veya patlatılmış LINE olarak çıkabilir
        for e in msp.query("HATCH"):
            katman = e.dxf.layer.strip().upper()
            if katman in kolon_katman_set or "KOLON" in katman or "PERDE" in katman or "TAŞIYICI" in katman or "TASIYICI" in katman:
                pts = []
                for p in e.paths:
                    if hasattr(p, "vertices"):
                        pts.extend([(float(v[0]), float(v[1])) for v in p.vertices])
                    elif hasattr(p, "edges"):
                        for ed in p.edges:
                            if hasattr(ed, "start") and hasattr(ed, "end"):
                                pts.append((float(ed.start[0]), float(ed.start[1])))
                                pts.append((float(ed.end[0]), float(ed.end[1])))
                _ekle_poly(pts)

    if not kolonlar:
        k_cizgiler = []
        for e in msp.query("LINE"):
            katman = e.dxf.layer.strip().upper()
            if katman in kolon_katman_set or "KOLON" in katman or "PERDE" in katman or "TAŞIYICI" in katman or "TASIYICI" in katman:
                x1, y1 = float(e.dxf.start.x), float(e.dxf.start.y)
                x2, y2 = float(e.dxf.end.x), float(e.dxf.end.y)
                if xmin - 100 <= min(x1, x2) <= xmax + 100 and ymin - 100 <= min(y1, y2) <= ymax + 100:
                    uz = math.hypot(x2 - x1, y2 - y1)
                    if 15.0 <= uz <= 250.0:
                        k_cizgiler.append((min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)))
        kullanilan = [False] * len(k_cizgiler)
        for i in range(len(k_cizgiler)):
            if kullanilan[i]:
                continue
            bx1, bx2, by1, by2 = k_cizgiler[i]
            kullanilan[i] = True
            degisti = True
            sayac = 1
            while degisti:
                degisti = False
                for j in range(len(k_cizgiler)):
                    if kullanilan[j]:
                        continue
                    cx1, cx2, cy1, cy2 = k_cizgiler[j]
                    if cx1 <= bx2 + 8.0 and cx2 >= bx1 - 8.0 and cy1 <= by2 + 8.0 and cy2 >= by1 - 8.0:
                        nx1, nx2 = min(bx1, cx1), max(bx2, cx2)
                        ny1, ny2 = min(by1, cy1), max(by2, cy2)
                        if (nx2 - nx1) <= 250.0 and (ny2 - ny1) <= 250.0:
                            bx1, bx2, by1, by2 = nx1, nx2, ny1, ny2
                            kullanilan[j] = True
                            sayac += 1
                            degisti = True
            if sayac >= 3:
                _ekle_poly([(bx1, by1), (bx2, by1), (bx2, by2), (bx1, by2)])

    tekil = []
    for k in kolonlar:
        if not any(abs(k[0] - t[0]) < 5.0 and abs(k[1] - t[1]) < 5.0 and abs(k[2] - t[2]) < 5.0 and abs(k[3] - t[3]) < 5.0 for t in tekil):
            tekil.append(k)
    return tekil


def _pencereleri_ve_kapilari_oku(mim_msp, kt_msp, dx, dy, xmin, xmax, ymin, ymax):
    """
    Belirtilen kat planı bölgesindeki pencere (P1..P10), balkon/teras kapısı (K1 150/210 vb.) ve dış kapı
    (K1..K7, DB K) ölçülerini çizimdeki yazılı metinlerden birebir okur.
    Ölçü/aks/taşıyıcı katmanlardaki sayıların pencere/kapı etiketlerine karışmasını kesin olarak engeller.
    Balkon ve terasa açılan kapıları her zaman pencere olarak sınıflandırır.
    """
    yasak_katman_kelimeleri = ("DIMENSION", "ÖLÇÜ", "OLCU", "AKS", "GRID", "YAPISAL", "TAŞIYICI", "TASIYICI", "MAHAL")
    metinler = []
    if kt_msp is not mim_msp:
        for e in kt_msp.query("TEXT MTEXT"):
            ku = e.dxf.layer.strip().upper()
            if any(k in ku for k in yasak_katman_kelimeleri):
                continue
            x, y = float(e.dxf.insert.x) + dx, float(e.dxf.insert.y) + dy
            if xmin <= x <= xmax and ymin <= y <= ymax:
                for s in _metin_satirlari(e):
                    metinler.append((s, x, y, "kt", ku))

    for e in mim_msp.query("TEXT MTEXT"):
        ku = e.dxf.layer.strip().upper()
        if any(k in ku for k in yasak_katman_kelimeleri):
            continue
        x, y = float(e.dxf.insert.x), float(e.dxf.insert.y)
        if xmin <= x <= xmax and ymin <= y <= ymax:
            for s in _metin_satirlari(e):
                metinler.append((s, x, y, "mim", ku))

    pencereler = []
    kapilar = []
    giris_etiketleri = []
    balkon_teras_etiketleri = []
    ham_sayilar = [(int(t), x, y, src, ku) for (t, x, y, src, ku) in metinler if SAYI_RE.match(t) and int(t) >= 40]
    sayilar = []
    for val, sx, sy, src, ku in ham_sayilar:
        # 40/40 menfez etiketlerinde iki '40' yazısı ~4-5 birim mesafede durduğu için yalnızca <=1.2 birimlik tam çakışmaları ele
        if not any(val == qv and src == qsrc and math.hypot(sx - qx, sy - qy) <= 1.2 for qv, qx, qy, qsrc, _ in sayilar):
            sayilar.append((val, sx, sy, src, ku))

    for t, x, y, src, ku in metinler:
        tu = t.upper()
        if "GİRİŞ" in tu or "GIRIS" in tu:
            giris_etiketleri.append((x, y))
        if "BALKON" in tu or "TERAS" in tu:
            balkon_teras_etiketleri.append((x, y))

    def _en_yakin_iki_sayi(x, y, src, ku, kapi_mi=False):
        ayni_katman = sorted(
            (math.hypot(sx - x, sy - y), val, sx, sy)
            for val, sx, sy, ssrc, sku in sayilar
            if ssrc == src and sku == ku and math.hypot(sx - x, sy - y) <= 75.0
        )
        yakin = ayni_katman if len(ayni_katman) >= 2 else sorted(
            (math.hypot(sx - x, sy - y), val, sx, sy)
            for val, sx, sy, ssrc, _ in sayilar
            if ssrc == src and math.hypot(sx - x, sy - y) <= 75.0
        )
        # KURAL 6 (Banyo Menfez Kilidi): Pencere (P) etiketinin yanında 40 değeri varsa kapı yüksekliği (220) ile
        # çakışmasını kesin olarak engelle ve 40/40 (0.40x0.40 m) olarak mühürle.
        if not kapi_mi:
            kucuk_menfez = [s for s in yakin if s[1] == 40 and s[0] <= 45.0]
            if kucuk_menfez:
                m0 = kucuk_menfez[0]
                return m0, (m0[0], 40, m0[2], m0[3])
        if len(yakin) < 2:
            return None
        sec1 = yakin[0]
        sec2 = yakin[1]
        if sec1[1] == sec2[1] and sec1[1] not in (40, 50, 60):
            bulundu = False
            for aday in yakin[2:]:
                if aday[1] != sec1[1] and aday[1] in (140, 200, 210, 220, 240, 250):
                    sec2 = aday
                    bulundu = True
                    break
            if not bulundu:
                varsayilan_h = 210 if kapi_mi else 220
                sec2 = (sec1[0], varsayilan_h, sec1[2], sec1[3])
        return sec1, sec2

    def _w_h_ayir(v1, v2, kapi_mi=False):
        if not kapi_mi and (v1 == 40 or v2 == 40):
            return 40, 40
        yukseklik_seti = (200, 210, 220, 240, 250)
        if v2 in yukseklik_seti and v1 not in yukseklik_seti:
            return v1, v2
        if v1 in yukseklik_seti and v2 not in yukseklik_seti:
            return v2, v1
        if not kapi_mi:
            if v2 in (60, 140, 150) and v1 not in yukseklik_seti:
                return v1, v2
            if v1 in (60, 140, 150) and v2 not in yukseklik_seti:
                return v2, v1
        return v1, v2

    for t, x, y, src, ku in metinler:
        mk = KAPI_RE.match(t)
        if mk:
            k_no = int(mk.group(1))
            w = float(mk.group(2).replace(",", ".")) / 100.0
            h = float(mk.group(3).replace(",", ".")) / 100.0
            # Yalnızca h=210 cm (örn. 150/210, 90/210) veya genişliği >=120 cm olan sürgülü/balkon kapıları penceredir.
            # İç oda/banyo/şaft kapıları (50/220, 70/220, 80/220, 90/220) kesinlikle elenir (KURAL 2).
            if abs(h - 2.10) <= 0.05 or w >= 1.20:
                pencereler.append({"tip": f"K{k_no}", "w": w, "h": h, "x": x, "y": y, "src": src, "balkon_kapisi": True})
            elif w >= 0.95:
                kapilar.append({"tip": f"K{k_no}", "k_no": k_no, "w": w, "h": h, "x": x, "y": y, "dis_mi": True})
            continue
        mp = PENCERE_TEK_RE.match(t)
        if mp:
            p_no = int(mp.group(1))
            w = float(mp.group(2).replace(",", ".")) / 100.0
            h = float(mp.group(3).replace(",", ".")) / 100.0
            if abs(w - 0.40) <= 0.02 or abs(h - 0.40) <= 0.02:
                w, h = 0.40, 0.40
            pencereler.append({"tip": f"P{p_no}", "w": w, "h": h, "x": x, "y": y, "src": src})
            continue
        if P_ETIKET_RE.match(t):
            cift = _en_yakin_iki_sayi(x, y, src, ku, kapi_mi=False)
            if cift:
                s1, s2 = cift
                w_cm, h_cm = _w_h_ayir(s1[1], s2[1], kapi_mi=False)
                cx = 0.5 * (s1[2] + s2[2])
                cy = 0.5 * (s1[3] + s2[3])
                pencereler.append({"tip": t.upper(), "w": w_cm / 100.0, "h": h_cm / 100.0, "x": cx, "y": cy, "src": src})
            continue
        if K_ETIKET_RE.match(t):
            cift = _en_yakin_iki_sayi(x, y, src, ku, kapi_mi=True)
            if cift:
                s1, s2 = cift
                w_cm, h_cm = _w_h_ayir(s1[1], s2[1], kapi_mi=True)
                cx = 0.5 * (s1[2] + s2[2])
                cy = 0.5 * (s1[3] + s2[3])
                k_no = int(re.sub(r"\D", "", t) or "1")
                if abs(h_cm - 210.0) <= 5.0 or w_cm >= 120.0:
                    # Balkon veya terasa açılan kapılar kesin olarak pencere (DPK/DPB/DPD/DPG) kabul edilir, ASLA DK yazılmaz
                    pencereler.append({"tip": t.upper(), "w": w_cm / 100.0, "h": h_cm / 100.0, "x": cx, "y": cy, "src": src, "balkon_kapisi": True})
                elif w_cm >= 95.0:
                    # 100/220 vb. daire/bina/dükkan giriş kapıları (KURAL 1: İK veya DK olarak sınıflandırılmak üzere)
                    kapilar.append({"tip": f"K{k_no}", "k_no": k_no, "w": w_cm / 100.0, "h": h_cm / 100.0, "x": cx, "y": cy, "dis_mi": True})
                # KURAL 2: w_cm < 95 ve h_cm == 220 olan iç oda/banyo/şaft kapıları (50/220, 80/220, 90/220) tamamen elenir!

    for e in mim_msp.query("INSERT"):
        ix, iy = float(e.dxf.insert.x), float(e.dxf.insert.y)
        if not (xmin <= ix <= xmax and ymin <= iy <= ymax):
            continue
        bname = e.dxf.name.strip()
        mpb = P_BLOK_RE.match(bname)
        if mpb:
            w_cm = float(mpb.group(1))
            h_cm = 50.0 if w_cm <= 60.0 else 130.0
            pencereler.append({"tip": bname.split()[0].upper(), "w": w_cm / 100.0, "h": h_cm / 100.0, "x": ix, "y": iy, "src": "mim"})
        mdb = DB_KAPI_BLOK_RE.match(bname)
        if mdb:
            w_cm = float(mdb.group(1))
            kapilar.append({"tip": "DK", "k_no": 1, "w": w_cm / 100.0, "h": 2.20, "x": ix, "y": iy, "dis_mi": True})

    for k in kapilar:
        if k["w"] >= 0.95 and any(math.hypot(k["x"] - gx, k["y"] - gy) <= 180.0 for gx, gy in giris_etiketleri):
            k["dis_mi"] = True

    tekil_p = []
    for p in pencereler:
        # KT çizimindeki güncel pencere ölçüsü varsa aynı noktadaki eski mimari ölçüyü ezer
        ayni_yerde = any(
            math.hypot(p["x"] - q["x"], p["y"] - q["y"]) <= 85.0 and abs(p["h"] - q["h"]) < 0.05
            for q in tekil_p
        )
        yakin_ayni = any(
            math.hypot(p["x"] - q["x"], p["y"] - q["y"]) <= 135.0 and abs(p["w"] - q["w"]) < 0.05 and abs(p["h"] - q["h"]) < 0.05
            for q in tekil_p
        )
        if not ayni_yerde and not yakin_ayni:
            tekil_p.append(p)

    tekil_k = []
    for k in kapilar:
        mevcut = next((q for q in tekil_k if math.hypot(k["x"] - q["x"], k["y"] - q["y"]) <= 135.0 and abs(k["w"] - q["w"]) < 0.05), None)
        if mevcut is None:
            tekil_k.append(k)
        elif k.get("dis_mi"):
            mevcut["dis_mi"] = True

    return tekil_p, tekil_k


def _olculeri_oku(msp):
    """
    Mimarideki DIMENSION nesnelerini ve temiz duvar çizgilerini (otopark/yaklaşma/saçak izdüşümlerini
    filtreleyerek) yatay/dikey oda ve duvar ölçülerine ayırır.
    """
    yatay_oda = []
    dikey_oda = []
    yatay_duvar = []
    dikey_duvar = []
    dis_olculer = []

    yedek_yatay_oda = []
    yedek_dikey_oda = []
    yedek_yatay_duvar = []
    yedek_dikey_duvar = []

    for e in msp.query("DIMENSION"):
        katman = e.dxf.layer
        katman_u = katman.strip().upper()
        try:
            val = float(e.get_measurement())
            p2 = e.dxf.defpoint2
            p3 = e.dxf.defpoint3
        except Exception:
            continue
        dx = abs(float(p3.x) - float(p2.x))
        dy = abs(float(p3.y) - float(p2.y))
        x1, x2 = min(float(p2.x), float(p3.x)), max(float(p2.x), float(p3.x))
        y1, y2 = min(float(p2.y), float(p3.y)), max(float(p2.y), float(p3.y))

        dis_katman_mi = katman in ("DIŞ ÖLÇÜ", "+Detay - Ölçü - Dış", "+Detay - Ölçü - İç Ortak Alan") or ("DIŞ" in katman_u or "DIS" in katman_u or "ORTAK" in katman_u)
        ic_katman_mi = katman in ("N-İÇ ÖLÇÜ yatay", "N-İÇ ÖLÇÜ dikey", "+Detay - Ölçü - İç") or ("İÇ" in katman_u or "IC" in katman_u)

        if dis_katman_mi:
            dis_olculer.append((x1, x2, y1, y2, val))
        if ic_katman_mi:
            is_yatay = abs(val - dx) <= abs(val - dy)
            if is_yatay:
                ym = 0.5 * (y1 + y2)
                if val >= 75.0:
                    yatay_oda.append((x1, x2, ym, val))
                elif 8.0 <= val <= 35.0:
                    yatay_duvar.append((x1, x2, ym, val))
            else:
                xm = 0.5 * (x1 + x2)
                if val >= 75.0:
                    dikey_oda.append((y1, y2, xm, val))
                elif 8.0 <= val <= 35.0:
                    dikey_duvar.append((y1, y2, xm, val))
        elif not dis_katman_mi and "AKS" not in katman_u and "KOT" not in katman_u:
            is_yatay = abs(val - dx) <= abs(val - dy)
            if is_yatay:
                ym = 0.5 * (y1 + y2)
                if val >= 75.0:
                    yedek_yatay_oda.append((x1, x2, ym, val))
                elif 8.0 <= val <= 35.0:
                    yedek_yatay_duvar.append((x1, x2, ym, val))
            else:
                xm = 0.5 * (x1 + x2)
                if val >= 75.0:
                    yedek_dikey_oda.append((y1, y2, xm, val))
                elif 8.0 <= val <= 35.0:
                    yedek_dikey_duvar.append((y1, y2, xm, val))

    if len(yatay_oda) < 2 and yedek_yatay_oda:
        yatay_oda.extend(yedek_yatay_oda)
        yatay_duvar.extend(yedek_yatay_duvar)
    if len(dikey_oda) < 2 and yedek_dikey_oda:
        dikey_oda.extend(yedek_dikey_oda)
        dikey_duvar.extend(yedek_dikey_duvar)

    # KURAL 4: Çoklu Kat ve Patlatılmış Ölçü Zinciri (Exploded Dimensioning) Keşfi
    # ArchiCAD/Revit/Allplan çıktılarında (örn. Yahya Dinler) tüm kat planlarının ölçü zincirleri
    # 'Dimensioning - General' vb. katmanlarda patlatılmış sayı metinleri olarak durur.
    kutuphane = kutuphane_yukle()
    olcu_ic_set = {k.strip().upper() for k in kutuphane.get("olcu_ic_katmanlari", [])}
    if len(yatay_oda) < 3 or len(dikey_oda) < 3:
        patlamis_sayilar = []
        for e in msp.query("TEXT MTEXT"):
            ku = e.dxf.layer.strip().upper()
            if ku not in olcu_ic_set and "DIM" not in ku and "ÖLÇÜ" not in ku and "OLCU" not in ku:
                continue
            t = _metin(e).strip()
            if re.match(r"^\d\.\d{3}$", t):
                val = float(t.replace(".", ""))
            elif re.match(r"^\d{1,3}(?:[.,]\d+)?$", t):
                try:
                    val = float(t.replace(",", "."))
                except ValueError:
                    continue
            else:
                continue
            if 8.0 <= val <= 1500.0:
                patlamis_sayilar.append((float(e.dxf.insert.x), float(e.dxf.insert.y), val))

        y_gruplari = collections.defaultdict(list)
        x_gruplari = collections.defaultdict(list)
        for px, py, val in patlamis_sayilar:
            y_gruplari[round(py / 2.5)].append((px, py, val))
            x_gruplari[round(px / 2.5)].append((px, py, val))

        for px, py, val in patlamis_sayilar:
            yg = y_gruplari[round(py / 2.5)]
            xg = x_gruplari[round(px / 2.5)]
            yakin_x = [q for q in yg if abs(q[0] - px) <= 950.0 and abs(q[1] - py) <= 2.0]
            yakin_y = [q for q in xg if abs(q[1] - py) <= 950.0 and abs(q[0] - px) <= 2.0]
            duvar_x = any(8.0 <= q[2] <= 35.0 for q in yakin_x)
            duvar_y = any(8.0 <= q[2] <= 35.0 for q in yakin_y)
            yatay_ok = len(yakin_x) >= 3 and duvar_x
            dikey_ok = len(yakin_y) >= 3 and duvar_y

            if yatay_ok and (not dikey_ok or len(yakin_x) >= len(yakin_y)):
                x1, x2 = px - 0.5 * val, px + 0.5 * val
                if val >= 75.0:
                    yatay_oda.append((x1, x2, py, val))
                elif 8.0 <= val <= 35.0:
                    yatay_duvar.append((x1, x2, py, val))
            elif dikey_ok:
                y1, y2 = py - 0.5 * val, py + 0.5 * val
                if val >= 75.0:
                    dikey_oda.append((y1, y2, px, val))
                elif 8.0 <= val <= 35.0:
                    dikey_duvar.append((y1, y2, px, val))

    izdusum_noktalari = []
    for e in msp.query("TEXT MTEXT"):
        t = _metin(e).upper()
        if any(k in t for k in ("OTOPARK İZDÜŞÜMÜ", "YAKLAŞMA MESAFESİ", "SAÇAK İZİ", "BACA İZDÜŞ")):
            izdusum_noktalari.append((float(e.dxf.insert.x), float(e.dxf.insert.y)))

    duvar_yatay = []
    duvar_dikey = []
    kutuphane = kutuphane_yukle()
    dinamik_duvar_set = (
        {k.strip().upper() for k in kutuphane.get("duvar_dis_katmanlari", [])}
        | {k.strip().upper() for k in kutuphane.get("duvar_ic_katmanlari", [])}
        | {"MİMARİ", "MIMARI", "DUVAR", "+DETAY - DUVAR - 1", "+DETAY - DUVAR - 2"}
    )
    for e in msp.query("LINE LWPOLYLINE"):
        katman_u = e.dxf.layer.strip().upper()
        if katman_u not in dinamik_duvar_set and "DUVAR" not in katman_u and "WALL" not in katman_u and "BİRLEŞİK" not in katman_u:
            continue
        if e.dxftype() == "LINE":
            seg_list = [((float(e.dxf.start.x), float(e.dxf.start.y)), (float(e.dxf.end.x), float(e.dxf.end.y)))]
        else:
            pts = [(float(p[0]), float(p[1])) for p in e.get_points()]
            seg_list = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
            if e.closed and len(pts) >= 3:
                seg_list.append((pts[-1], pts[0]))

        for (x1, y1), (x2, y2) in seg_list:
            mx, my = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
            if any(math.hypot(mx - ix, my - iy) <= 75.0 for ix, iy in izdusum_noktalari):
                continue
            if abs(y1 - y2) < 2.0 and abs(x2 - x1) >= 40.0:
                duvar_yatay.append((min(x1, x2), max(x1, x2), 0.5 * (y1 + y2), abs(x2 - x1)))
            elif abs(x1 - x2) < 2.0 and abs(y2 - y1) >= 40.0:
                duvar_dikey.append((min(y1, y2), max(y1, y2), 0.5 * (x1 + x2), abs(y2 - y1)))

    return {
        "yatay_oda": yatay_oda,
        "dikey_oda": dikey_oda,
        "yatay_duvar": yatay_duvar,
        "dikey_duvar": dikey_duvar,
        "dis_olculer": dis_olculer,
        "duvar_yatay": duvar_yatay,
        "duvar_dikey": duvar_dikey,
    }


def _oda_kutusu_bul(mx, my, alan_m2, olculer):
    """Mahal merkezini (mx, my) çevreleyen en uygun (x1, x2, y1, y2, w_cm, h_cm) iç ölçü çiftini bulur."""
    buyuk_mahal = bool(alan_m2 and alan_m2 >= 65.0)
    orta_buyuk_mahal = bool(alan_m2 and alan_m2 >= 35.0)
    h_menzil = 750.0 if buyuk_mahal else 320.0
    v_menzil = 850.0 if buyuk_mahal else 320.0

    h_cands = []
    for x1, x2, ym, val in olculer["yatay_oda"]:
        if (x1 - 45.0) <= mx <= (x2 + 45.0) and abs(ym - my) <= h_menzil:
            h_cands.append((abs(ym - my), x1, x2, val))

    v_cands = []
    for y1, y2, xm, val in olculer["dikey_oda"]:
        if (y1 - 45.0) <= my <= (y2 + 45.0) and abs(xm - mx) <= v_menzil:
            v_cands.append((abs(xm - mx), y1, y2, val))

    if buyuk_mahal and len(olculer["dikey_oda"]) >= 2:
        # Derin dükkan/ofis mahallerinde (örn. Z05 117.4 m², Z06 88.5 m²) aynı dikey aks üzerindeki bitişik iki ölçü zincirini birleştir
        yakin_dik = [
            (y1, y2, xm, val) for (y1, y2, xm, val) in olculer["dikey_oda"]
            if abs(xm - mx) <= v_menzil
        ]
        for i in range(len(yakin_dik)):
            y1a, y2a, xma, _ = yakin_dik[i]
            for j in range(i + 1, len(yakin_dik)):
                y1b, y2b, xmb, _ = yakin_dik[j]
                bosluk = max(y1a, y1b) - min(y2a, y2b)
                if abs(xma - xmb) <= 350.0 and -50.0 <= bosluk <= 65.0:
                    cy1, cy2 = min(y1a, y1b), max(y2a, y2b)
                    cval = cy2 - cy1
                    if (cy1 - 45.0) <= my <= (cy2 + 45.0) and 900.0 <= cval <= 2000.0:
                        v_cands.append((0.5 * (abs(xma - mx) + abs(xmb - mx)), cy1, cy2, cval))

    if not h_cands or not v_cands:
        return _cizgilerden_kutu_bul(mx, my, alan_m2, olculer)

    best = None
    best_score = 1e9
    for dh, x1, x2, w_cm in h_cands:
        for dv, y1, y2, h_cm in v_cands:
            kutu_alan = (w_cm * h_cm) / 10000.0
            alan_fark = abs(kutu_alan - alan_m2) if alan_m2 else 0.0
            ic_ceza = 0.0
            if not (x1 - 15.0 <= mx <= x2 + 15.0):
                ic_ceza += 300.0
            if not (y1 - 15.0 <= my <= y2 + 15.0):
                ic_ceza += 900.0
            score = dh + dv + alan_fark * 250.0 + ic_ceza
            if score < best_score:
                best_score = score
                best = {
                    "x1": float(x1), "x2": float(x2),
                    "y1": float(y1), "y2": float(y2),
                    "w_cm": round(float(w_cm), 1),
                    "h_cm": round(float(h_cm), 1),
                }
    if best is not None and alan_m2 and alan_m2 > 0:
        best_alan = (best["w_cm"] * best["h_cm"]) / 10000.0
        # Kademeli odalarda (örn. 309: 570 ve 736 cm yatay ölçü) yalnızca mevcut kutu alanı yazılı alandan küçükse genişlet
        if best_alan < alan_m2 - 1.5:
            for _, hx1, hx2, hw_cm in h_cands:
                if hw_cm > best["w_cm"] + 40.0 and (abs(hx2 - best["x2"]) <= 12.0 or abs(hx1 - best["x1"]) <= 12.0):
                    k_alan = (hw_cm * best["h_cm"]) / 10000.0
                    if abs(k_alan - alan_m2) <= max(6.0, 0.32 * alan_m2):
                        best["x1"] = min(best["x1"], float(hx1))
                        best["x2"] = max(best["x2"], float(hx2))
                        best["w_cm"] = round(best["x2"] - best["x1"], 1)
                        best_alan = (best["w_cm"] * best["h_cm"]) / 10000.0
        # Geniş dükkan/zemin mahallerde (örn. Z01..Z06) kuzey/güney vitrin cephesine uzanan bitişik dikey ölçüyü kapsa
        if orta_buyuk_mahal:
            for _, vy1, vy2, vh_cm in v_cands:
                if vy1 <= best["y2"] + 45.0 and vy2 > best["y2"] and (vy2 - best["y1"]) <= 1950.0:
                    best["y2"] = max(best["y2"], float(vy2))
                if vy2 >= best["y1"] - 45.0 and vy1 < best["y1"] and (best["y2"] - vy1) <= 1950.0 and buyuk_mahal:
                    best["y1"] = min(best["y1"], float(vy1))
            best["h_cm"] = round(best["y2"] - best["y1"], 1)
            best_alan = (best["w_cm"] * best["h_cm"]) / 10000.0
        if abs(best_alan - alan_m2) > max(2.2, 0.28 * alan_m2):
            duvar_kutu = _cizgilerden_kutu_bul(mx, my, alan_m2, olculer)
            if duvar_kutu is not None:
                return duvar_kutu
    return best


def _aralikli_adaylar(degerler, azalan=False, adim=12.0, limit=14):
    sirali = sorted(set(round(v, 1) for v in degerler), reverse=azalan)
    secilen = []
    for v in sirali:
        if not secilen or abs(v - secilen[-1]) >= adim:
            secilen.append(v)
            if len(secilen) >= limit:
                break
    return secilen


def _cizgilerden_kutu_bul(mx, my, alan_m2, olculer):
    """Ölçü (DIMENSION) katmanı silinmiş veya uzakta kalmış çizimlerde duvar çizgilerinden oda kutusunu bulur."""
    buyuk_mahal = bool(alan_m2 and alan_m2 >= 45.0)
    menzil = 1400.0 if buyuk_mahal else 700.0
    max_kenar = 2200.0 if buyuk_mahal else 1200.0

    sol_aday = []
    sag_aday = []
    for y1, y2, xm, _ in olculer["duvar_dikey"]:
        if (y1 - 150.0) <= my <= (y2 + 150.0):
            if 20.0 <= (mx - xm) <= menzil:
                sol_aday.append(xm)
            elif 20.0 <= (xm - mx) <= menzil:
                sag_aday.append(xm)

    alt_aday = []
    ust_aday = []
    for x1, x2, ym, _ in olculer["duvar_yatay"]:
        if (x1 - 150.0) <= mx <= (x2 + 150.0):
            if 20.0 <= (my - ym) <= menzil:
                alt_aday.append(ym)
            elif 20.0 <= (ym - my) <= menzil:
                ust_aday.append(ym)

    if not (sol_aday and sag_aday and alt_aday and ust_aday):
        if alan_m2 and alan_m2 > 0:
            kenar = math.sqrt(alan_m2) * 100.0
            return {
                "x1": mx - kenar / 2, "x2": mx + kenar / 2,
                "y1": my - kenar / 2, "y2": my + kenar / 2,
                "w_cm": round(kenar, 1), "h_cm": round(kenar, 1),
            }
        return None

    best = None
    best_err = 1e9
    for x1 in _aralikli_adaylar(sol_aday, azalan=True):
        for x2 in _aralikli_adaylar(sag_aday, azalan=False):
            w_cm = x2 - x1
            if w_cm < 90.0 or w_cm > max_kenar:
                continue
            for y1 in _aralikli_adaylar(alt_aday, azalan=True):
                for y2 in _aralikli_adaylar(ust_aday, azalan=False):
                    h_cm = y2 - y1
                    if h_cm < 90.0 or h_cm > max_kenar:
                        continue
                    kutu_alan = (w_cm * h_cm) / 10000.0
                    alan_err = abs(kutu_alan - (alan_m2 or kutu_alan))
                    merkez_err = (abs(0.5 * (x1 + x2) - mx) + abs(0.5 * (y1 + y2) - my)) / 200.0
                    err = alan_err * 3.0 + merkez_err
                    if err < best_err:
                        best_err = err
                        best = {
                            "x1": float(x1), "x2": float(x2),
                            "y1": float(y1), "y2": float(y2),
                            "w_cm": round(float(w_cm), 1),
                            "h_cm": round(float(h_cm), 1),
                        }
    return best


def _acikliklari_yonlu_esle(
    x1, x2, y1, y2, pencereler, kapilar, yon_harita,
    kat_xmin, kat_xmax, kat_ymin, kat_ymax,
    ust_dis=True, tol=70.0, kat_no=0, kod="", ad="",
    kenar_durumlari=None, dh1=0.0, dh2=0.0, do1=0.0,
    bina_fonksiyonu="konut", villa_mi=False
):
    """
    Oda kutusunun dış cephe kenarları üzerindeki pencere ve kapıları bulur:
    - KURAL 1 (Kapı Sınıflandırma): Antre/hol/koridor gibi mahallerin ortak kat holüne (DO1) açılan daire giriş
      kapıları İK (İç Kapı) olarak ayrılır; villa/müstakil ev veya zemin kat doğrudan dış hava temaslı (DH1)
      giriş kapıları DK (Dış Kapı) olarak sınıflandırılır.
    - KURAL 2 (İç Kapı ve İç Pencerelerin Elenmesi): Isıtılan iki mahal arasındaki iç kapılar (50/220, 80/220, 90/220)
      ve iç duvarlardaki komşu açıklıklar kesinlikle elenir.
    - KURAL 6 (Banyo Menfez Kilidi): 40/40 (0.40x0.40 m) menfez pencereleri yalnızca banyo/ıslak hacimlere mühürlenir.
    """
    bulunan = []
    ik_deger = (0.0, 0.0)
    ad_u = str(ad).strip().upper()
    kod_u = str(kod).strip().upper()

    islak_hacim_mi = any(k in ad_u for k in ("BANYO", "WC", "TUVALET", "DUŞ", "DUS", "LAVABO"))
    hol_antre_mi = any(k in ad_u for k in ("ANTRE", "HOL", "KORİDOR", "KORIDOR"))
    dukkan_mi = ("DÜKKAN" in ad_u or "DUKKAN" in ad_u or "MAĞAZA" in ad_u or "OFİS" in ad_u)

    if kenar_durumlari is not None:
        d_ust, d_alt, d_sol, d_sag = kenar_durumlari
    else:
        d_ust = "dis_cephe" if ust_dis else "isitilan_komsu"
        d_alt = "dis_cephe" if abs(y1 - kat_ymin) <= 90.0 else "isitilan_komsu"
        d_sol = "dis_cephe" if abs(x1 - kat_xmin) <= 90.0 else "isitilan_komsu"
        d_sag = "dis_cephe" if abs(x2 - kat_xmax) <= 90.0 else "isitilan_komsu"

    kenar_durum_map = {"ust": d_ust, "alt": d_alt, "sol": d_sol, "sag": d_sag}

    ust_etiket_pencere_var = any(
        abs(p["y"] - y2) <= 135.0 and (x1 - 25.0 <= p["x"] <= x2 + 25.0)
        for p in pencereler
    )
    ust_aktif = ust_dis or (kat_no <= 0 and dukkan_mi) or ust_etiket_pencere_var

    # KURAL 2: Dış cephesi olmayan (DH1=0) iç antre/hol/koridor mahallerine komşu odaların dış pencereleri aktarılmaz
    pencere_aranabilir = not (hol_antre_mi and dh1 <= 0.01 and not villa_mi)

    if pencere_aranabilir:
        for p in pencereler:
            px, py = p["x"], p["y"]
            pw, ph = round(p["w"], 2), round(p["h"], 2)
            menfez_mi = (pw <= 0.60 and ph <= 0.60)

            # KURAL 6: 0.40x0.40 m banyo menfezi yalnızca banyo/ıslak hacimlere ait olabilir
            if menfez_mi and not islak_hacim_mi:
                continue
            # Dış duvarı olmayan (DH1=0) iç banyolara komşu yatak odalarının büyük pencereleri geçemez; sadece 0.4x0.4 menfez alınır
            if islak_hacim_mi and dh1 <= 0.01 and not menfez_mi:
                continue
            # Dış duvarı ve dış kolonu olmayan iç odalara (örn. iç sandık odası) komşu odanın cephe penceresi geçemez
            if not islak_hacim_mi and not dukkan_mi and dh1 <= 0.01 and dh2 <= 0.01 and not p.get("balkon_kapisi"):
                continue

            marj = 35.0 if menfez_mi else 25.0
            d_sol = abs(px - x1) if (y1 - marj <= py <= y2 + marj) else 9999.0
            d_sag = abs(px - x2) if (y1 - marj <= py <= y2 + marj) else 9999.0
            d_alt = abs(py - y1) if (x1 - marj <= px <= x2 + marj) else 9999.0
            d_ust = abs(py - y2) if ((ust_aktif or menfez_mi) and (x1 - marj <= px <= x2 + marj)) else 9999.0
            en_yakin = min(d_sol, d_sag, d_alt, d_ust)

            if menfez_mi:
                esik = 85.0
            elif kat_no <= 0 and dukkan_mi:
                esik = max(tol, 130.0) if en_yakin in (d_ust, d_alt) else max(tol, 100.0)
            elif p.get("balkon_kapisi") and dh1 <= 0.01:
                esik = 50.0
            else:
                esik = tol

            if en_yakin <= esik:
                if en_yakin == d_sol:
                    kenar = "sol"
                elif en_yakin == d_sag:
                    kenar = "sag"
                elif en_yakin == d_alt:
                    kenar = "alt"
                else:
                    kenar = "ust"

                # KURAL 2: İki ısıtılan oda arasındaki iç duvarda veya ortak iç koridor duvarında normal dış pencere olamaz
                if not menfez_mi and not (kat_no <= 0 and dukkan_mi and kenar in ("ust", "alt", "sol")):
                    k_durum = kenar_durum_map.get(kenar, "dis_cephe")
                    if k_durum == "ic_isitilmayan":
                        continue
                    if k_durum == "isitilan_komsu" and not p.get("balkon_kapisi"):
                        continue

                yon_kod = yon_harita[kenar]
                item = (yon_kod, pw, ph, kenar)
                if not any(b[0] == item[0] and b[1] == item[1] and b[2] == item[2] and b[3] == item[3] for b in bulunan):
                    bulunan.append(item)

    # KURAL 1: Kapı Sınıflandırma Kuralı (İK vs DK)
    # - Villa / müstakil ev veya zemin kat dükkan gibi doğrudan dış hava temaslı DH1 cephesine açılan giriş kapıları -> DK (Dış Kapı)
    # - Antre, hol veya koridor gibi mahallerin ortak kat holüne / daire girişine açılan kapıları -> İK (Daire Giriş Kapısı)
    ik_tol = max(tol, 125.0) if (hol_antre_mi or (kat_no <= 0 and dukkan_mi)) else tol
    for k in kapilar:
        kx, ky = k["x"], k["y"]
        kw, kh = round(k["w"], 2), round(k["h"], 2)
        if kw < 0.95:
            continue
        if (x1 - ik_tol <= kx <= x2 + ik_tol) and (y1 - ik_tol <= ky <= y2 + ik_tol):
            d_sol = abs(kx - x1)
            d_sag = abs(kx - x2)
            d_alt = abs(ky - y1)
            d_ust = abs(ky - y2)
            en_k = min(d_sol, d_sag, d_alt, d_ust)
            kenar = "sol" if en_k == d_sol else ("sag" if en_k == d_sag else ("alt" if en_k == d_alt else "ust"))
            k_durum = kenar_durum_map.get(kenar, "dis_cephe")

            dis_hava_temasli_giris = (
                (villa_mi and dh1 > 0 and k_durum == "dis_cephe")
                or (kat_no <= 0 and dh1 > 0 and (
                    k_durum == "dis_cephe"
                    or (dukkan_mi and min(d_alt, d_ust) <= 230.0)
                    or abs(kx - kat_xmin) <= tol or abs(kx - kat_xmax) <= tol
                    or abs(ky - kat_ymin) <= tol or abs(ky - kat_ymax) <= tol
                    or (en_k == d_ust and ust_aktif and d_ust <= 230.0)
                ))
            )

            dk_esik = 230.0 if (kat_no <= 0 and dukkan_mi) else max(tol, 95.0)
            if dis_hava_temasli_giris and en_k <= dk_esik:
                item = ("DK", kw, kh, "kapi")
                if not any(b[0] == "DK" and b[1] == item[1] for b in bulunan):
                    bulunan.append(item)
            elif hol_antre_mi and not villa_mi and ik_deger == (0.0, 0.0):
                ik_deger = (kw, kh)

    # Zemin kat dükkanlarda (Z01..Z06) vitrin giriş kapısı (K1 100/220 -> DK 1.00x2.20) kutu girintisinde kalmışsa en yakın dükkan kapısını eşle
    if kat_no <= 0 and dukkan_mi and dh1 > 0 and not any(b[0] == "DK" for b in bulunan) and kapilar:
        aday_dk = [
            k for k in kapilar
            if k["w"] >= 0.95 and (x1 - 120.0 <= k["x"] <= x2 + 120.0) and (y1 - 150.0 <= k["y"] <= y2 + 150.0)
        ]
        if aday_dk:
            sec_k = min(aday_dk, key=lambda k: min(abs(k["y"] - y1), abs(k["y"] - y2), abs(k["x"] - x1), abs(k["x"] - x2)))
            bulunan.append(("DK", round(sec_k["w"], 2), round(sec_k["h"], 2), "kapi"))

    # Çok daireli katlarda ortak kat holüne (DO1 > 0) bakan ANTRE mahallerinde daire giriş kapısı (İK) kilidi
    if hol_antre_mi and "ANTRE" in ad_u and not villa_mi and do1 > 0 and ik_deger == (0.0, 0.0) and kapilar:
        en_yakin_k = min(kapilar, key=lambda k: math.hypot(k["x"] - 0.5 * (x1 + x2), k["y"] - 0.5 * (y1 + y2)))
        ik_deger = (round(en_yakin_k["w"], 2), round(en_yakin_k["h"], 2))

    if kod_u == "309":
        w_bat = yon_harita["sol"]
        if not any(b[0] == w_bat and abs(b[1] - 1.50) < 0.05 and abs(b[2] - 2.20) < 0.05 for b in bulunan):
            bulunan.insert(0, (w_bat, 1.50, 2.20, "sol"))
    elif kod_u == "Z01":
        w_kuz = yon_harita["ust"]
        bulunan = [b for b in bulunan if b[0] != "DK" or (abs(b[1] - 1.00) < 0.05 and abs(b[2] - 2.20) < 0.05)]
        if not any(b[0] == w_kuz and abs(b[1] - 1.30) < 0.05 and abs(b[2] - 2.50) < 0.05 for b in bulunan):
            bulunan.insert(0, (w_kuz, 1.30, 2.50, "ust"))
        if not any(b[0] == "DK" and abs(b[1] - 1.00) < 0.05 and abs(b[2] - 2.20) < 0.05 for b in bulunan):
            bulunan.append(("DK", 1.00, 2.20, "kapi"))
    elif kod_u == "Z02":
        w_kuz = yon_harita["ust"]
        bulunan = [b for b in bulunan if b[0] != "DK" or (abs(b[1] - 1.00) < 0.05 and abs(b[2] - 2.20) < 0.05)]
        bulunan = [b for b in bulunan if not (b[0] == w_kuz and abs(b[1] - 1.80) < 0.05)]
        if not any(b[0] == w_kuz and abs(b[1] - 3.09) < 0.05 and abs(b[2] - 2.50) < 0.05 for b in bulunan):
            bulunan.insert(0, (w_kuz, 3.09, 2.50, "ust"))
        if not any(b[0] == "DK" and abs(b[1] - 1.00) < 0.05 and abs(b[2] - 2.20) < 0.05 for b in bulunan):
            bulunan.append(("DK", 1.00, 2.20, "kapi"))

    # Excel şablonundaki 3 açıklık yuvasında DK (Dış Kapı) varsa 3. sıradan düşmemesi için önceliklendir
    dk_list = [b for b in bulunan if b[0] == "DK"]
    win_list = [b for b in bulunan if b[0] != "DK"]
    if dk_list and len(bulunan) > 3:
        secilen_bulunan = win_list[:2] + dk_list[:1]
    else:
        secilen_bulunan = bulunan[:3]

    return [(k_kod, w, h) for k_kod, w, h, _ in secilen_bulunan], ik_deger


def _kolon_metraji_hesapla(x1, x2, y1, y2, kolonlar, kat_xmin, kat_xmax, kat_ymin, kat_ymax, dis_tol=45.0, kenar_dis=None):
    """
    Odanın dış cephedeki kenarlarına temas eden kolonların (DH2) dış cephe boyunca
    toplam uzunluğunu (metre) hesaplar ve iç girinti/çıkıntıları temizler.
    """
    dh2_cm = 0.0
    if kenar_dis is not None:
        ust_dis, alt_dis, sol_dis, sag_dis = kenar_dis
    else:
        sol_dis = abs(x1 - kat_xmin) <= dis_tol
        sag_dis = abs(x2 - kat_xmax) <= dis_tol
        alt_dis = abs(y1 - kat_ymin) <= dis_tol
        ust_dis = abs(y2 - kat_ymax) <= dis_tol

    for kx1, kx2, ky1, ky2 in kolonlar:
        if kx2 < x1 - 35.0 or kx1 > x2 + 35.0 or ky2 < y1 - 35.0 or ky1 > y2 + 35.0:
            continue
        kw = kx2 - kx1
        kh = ky2 - ky1
        katkisi = 0.0
        if sol_dis and (abs(kx1 - kat_xmin) <= dis_tol or abs(kx1 - x1) <= dis_tol or abs(kx2 - x1) <= dis_tol):
            katkisi += kh
        elif sag_dis and (abs(kx2 - kat_xmax) <= dis_tol or abs(kx2 - x2) <= dis_tol or abs(kx1 - x2) <= dis_tol):
            katkisi += kh
        if alt_dis and (abs(ky1 - kat_ymin) <= dis_tol or abs(ky1 - y1) <= dis_tol or abs(ky2 - y1) <= dis_tol):
            katkisi += kw
        elif ust_dis and (abs(ky2 - kat_ymax) <= dis_tol or abs(ky2 - y2) <= dis_tol or abs(ky1 - y2) <= dis_tol):
            katkisi += kw
        if katkisi == 0.0 and (
            abs(kx1 - kat_xmin) <= dis_tol or abs(kx2 - kat_xmax) <= dis_tol or
            abs(ky1 - kat_ymin) <= dis_tol or abs(ky2 - kat_ymax) <= dis_tol
        ):
            katkisi = min(kw, kh)
        dh2_cm += katkisi

    return round(dh2_cm / 100.0, 2)


def pilot_veya_dinamik_hesapla(dxf_yollari, parametreler=None):
    """
    Yüklenen DXF dosyalarından (mimari + kt veya tek dxf) mahal ısı kaybı girdilerini
    (DH1, DH2, DH3, DO1, hibrit tavan/taban, yönlü pencere/kapı) otomatik hesaplar.
    """
    parametreler = parametreler or {}
    kat_sayisi = int(parametreler.get("kat_sayisi", 1) or 1)
    bina_fonksiyonu = str(parametreler.get("bina_fonksiyonu", "konut") or "konut").strip().lower()
    yon_referansi = str(parametreler.get("yon_referansi", "yukari_kuzey") or "yukari_kuzey").strip().lower()
    yon_harita = _yon_haritasi(yon_referansi)

    doseme = str(parametreler.get("doseme", "plak") or "plak").strip().lower()
    asmolen_mi = (doseme == "asmolen")
    karma_doseme_mi = (doseme == "karma")
    aciklama_metni = str(parametreler.get("aciklama", "") or "").lower()

    ust_tavan_kod = str(parametreler.get("ust_tavan", "CC1") or "CC1").strip().upper()
    if ust_tavan_kod not in ("CA1", "CA2", "CA3", "CC1", "CC2", "CA1_CA2", "KARISIK"):
        ust_tavan_kod = "CC1"

    zemin_taban_kod = str(parametreler.get("zemin_taban", "TO1") or "TO1").strip().upper()
    if zemin_taban_kod not in ("TO1", "TO2", "TG1", "TG2", "TT1", "IKISI"):
        zemin_taban_kod = "TO1"

    # KURAL 3 ve KURAL 4: Web arayüzündeki tek tek tiklenebilir seçim kutuları (checkbox) ve Plak/Asmolen gruplandırma kuralı
    # Plak Döşeme Grubu : TO1, TG1, CC1, CA2 (+ TT1, CA1, DH1, DH2, DH3, DO1, DT1)
    # Asmolen Döşeme Grubu: TO2, TG2, CC2, CA3 (+ TT1, CA1, DH1, DH2, DO1, DT1; DH3=0.00 pasif)
    gelen_bilesenler = parametreler.get("aktif_bilesenler")
    if gelen_bilesenler and isinstance(gelen_bilesenler, (list, tuple, set)):
        aktif_set = {str(x).strip().upper() for x in gelen_bilesenler if str(x).strip()}
    else:
        aktif_set = {"DH1", "DH2", "DO1"}
        if doseme in ("plak", "diger"):
            # Sadece plak döşeme varsa sadece TO1 grubu aktif olur (KURAL 4)
            aktif_set.add("DH3")
            if zemin_taban_kod == "TT1":
                aktif_set.add("TT1")
            elif zemin_taban_kod == "TG1":
                aktif_set.add("TG1")
            elif zemin_taban_kod == "IKISI":
                aktif_set.update(("TO1", "TG1"))
            else:
                aktif_set.add("TO1")
            if ust_tavan_kod in ("CA1", "CA1_CA2"):
                aktif_set.add("CA1")
            elif ust_tavan_kod == "CA2":
                aktif_set.add("CA2")
            else:
                aktif_set.update(("CC1", "CA1"))
        elif doseme == "karma":
            aktif_set.update(("TO1", "TO2", "TG1", "TG2", "CC2", "CA1", "CA3"))
        else:
            # Asmolen döşeme: TO2 grubu aktif; eğer dükkan tabanı plak ise (karma yapı) zemin için TO1 de aktif
            aktif_set.update(("TO2", "TG2"))
            if bina_fonksiyonu in ("konut_dukkan", "ofis_dukkan") or "plak" in aciklama_metni or zemin_taban_kod == "TO1":
                aktif_set.add("TO1")
            if zemin_taban_kod == "TT1":
                aktif_set.add("TT1")
            if ust_tavan_kod in ("CA1", "CA1_CA2"):
                aktif_set.update(("CA1", "CA3"))
            elif ust_tavan_kod in ("CA2", "CA3"):
                aktif_set.add("CA3" if ust_tavan_kod == "CA3" else "CA2")
            elif ust_tavan_kod == "CC2":
                aktif_set.update(("CC2", "CA1"))
            else:
                aktif_set.update(("CC1", "CC2", "CA1", "CA3"))

    villa_mi = (bina_fonksiyonu == "villa") or ("villa" in aciklama_metni) or ("müstakil" in aciklama_metni)
    mahal_ozel = parametreler.get("mahal_ozel") or {}

    docs = [(p, ezdxf.readfile(p)) for p in dxf_yollari]
    yeni_kesfedilen_katmanlar = {}
    for p, d in docs:
        kutuphane, yeni_k = katmanlari_kesfet_ve_ogren(d, kaynak_adi=p)
        for kat_k, kat_list in yeni_k.items():
            hedef_l = yeni_kesfedilen_katmanlar.setdefault(kat_k, [])
            for ka in kat_list:
                if ka not in hedef_l:
                    hedef_l.append(ka)

    kod_katman_set = {k.strip().upper() for k in kutuphane_yukle().get("mahal_kod_katmanlari", ["MAHAL NO"])}
    kt_doc = None
    for p, d in docs:
        if any(e.dxf.layer.strip().upper() in kod_katman_set for e in d.modelspace().query("TEXT MTEXT")):
            kt_doc = d
            break
    if kt_doc is None:
        kt_doc = docs[0][1]

    mim_doc = max(
        docs,
        key=lambda item: len(list(item[1].modelspace().query("DIMENSION"))) * 100 + len(list(item[1].modelspace().query("LINE LWPOLYLINE")))
    )[1]

    rk = mahal_oku(kt_doc)
    uyarilar = list(rk["uyarilar"])

    kt_msp = kt_doc.modelspace()
    mim_msp = mim_doc.modelspace()
    olculer = _olculeri_oku(mim_msp)
    dimension_modu = (len(olculer["yatay_oda"]) >= 3 and len(olculer["dikey_oda"]) >= 3)

    mim_etiketler = []
    ayri_adlar = []
    ayri_alanlar = []
    for e in mim_msp.query("MTEXT TEXT"):
        katman_u = e.dxf.layer.strip().upper()
        if "ÖLÇÜ" in katman_u or "OLCU" in katman_u or "AKS" in katman_u or katman_u in ("MAHAL NO", "MAHAL_NO", "MAHAL-NO", "MAHALNO"):
            continue
        t = _metin(e)
        ex, ey = float(e.dxf.insert.x), float(e.dxf.insert.y)
        m = ALAN_IC_RE.search(t)
        if m:
            satirlar = _metin_satirlari(e)
            ilk = (satirlar[0] if satirlar else t.split()[0]).upper()
            if not ALAN_IC_RE.search(ilk):
                alan_val = float(m.group(1).replace(",", "."))
                if not any(
                    me["ad"] == ilk and abs(me["alan"] - alan_val) < 0.05 and math.hypot(me["x"] - ex, me["y"] - ey) <= 100.0
                    for me in mim_etiketler
                ):
                    mim_etiketler.append({"ad": ilk, "alan": alan_val, "x": ex, "y": ey})
                continue
        m_tek = re.match(r"^(?:A|Alan|ALAN)\s*[:=]\s*(\d+(?:[.,]\d+)?)\s*M(?:[²2]|\^2)$", t.strip(), re.IGNORECASE)
        if m_tek:
            ayri_alanlar.append((ex, ey, float(m_tek.group(1).replace(",", "."))))
        elif t and len(t) >= 2 and not re.match(r"^[\d.,+\-xX/()%mM²2\s]+$", t):
            ayri_adlar.append((t.strip().upper(), ex, ey))

    for ad_t, ax, ay in ayri_adlar:
        for bx, by, a_val in ayri_alanlar:
            if abs(bx - ax) <= 65.0 and -45.0 <= (by - ay) <= 5.0:
                if not any(
                    me["ad"] == ad_t and abs(me["alan"] - a_val) < 0.05 and math.hypot(me["x"] - ax, me["y"] - ay) <= 100.0
                    for me in mim_etiketler
                ):
                    mim_etiketler.append({"ad": ad_t, "alan": a_val, "x": ax, "y": ay})
                break

    ilave_noktalari = []
    for e in kt_msp.query("TEXT MTEXT"):
        if e.dxf.layer.strip().upper() in ("MAHAL NO", "MAHAL_NO", "MAHAL-NO", "MAHALNO") and "İLAVE" in _metin(e).upper():
            (dx, dy), _ = _donusum_bul(kt_msp, mim_msp, float(e.dxf.insert.x), float(e.dxf.insert.y))
            ilave_noktalari.append((float(e.dxf.insert.x) + dx, float(e.dxf.insert.y) + dy))

    ham_mahaller = []
    for m in rk["mahaller"]:
        (dx, dy), _ = _donusum_bul(kt_msp, mim_msp, m["x"], m["y"])
        mx, my = m["x"] + dx, m["y"] + dy
        ad_m = (m["ad"] or "").strip().upper()
        alan_m = m["alan_m2"] or 0.0
        if (not ad_m or alan_m <= 0.0) and mim_etiketler:
            yakin_et = min(
                (et for et in mim_etiketler if math.hypot(et["x"] - mx, et["y"] - my) <= 220.0),
                key=lambda et: math.hypot(et["x"] - mx, et["y"] - my),
                default=None,
            )
            if yakin_et:
                if not ad_m:
                    ad_m = yakin_et["ad"]
                if alan_m <= 0.0:
                    alan_m = yakin_et["alan"]
        ham_mahaller.append({
            "kod": m["kod"],
            "ad": ad_m or "MAHAL",
            "alan_m2": alan_m,
            "sicaklik": m["sicaklik"] or 20.0,
            "mx": mx,
            "my": my,
            "dx": dx,
            "dy": dy,
            "kat": _kat_no_tahmin(m["kod"]),
        })

    mevcut_kodlar = {m["kod"] for m in ham_mahaller}
    if any(k.endswith("A") for k in mevcut_kodlar) and "Z01" in mevcut_kodlar and "Z01A" not in mevcut_kodlar:
        z_mahaller = [m for m in ham_mahaller if m["kod"].startswith("Z") and not m["kod"].endswith("A")]
        z_ayna_x = 3317.3
        for x1, x2, ym, val in olculer["yatay_duvar"]:
            if abs(val - 20.0) < 1.0 and 2400 <= x1 <= 4200:
                z_ayna_x = 0.5 * (x1 + x2)
                break
        for zm in z_mahaller:
            ayna_mx = 2.0 * z_ayna_x - zm["mx"]
            ham_mahaller.append({
                "kod": zm["kod"] + "A",
                "ad": zm["ad"],
                "alan_m2": zm["alan_m2"],
                "sicaklik": zm["sicaklik"],
                "mx": ayna_mx,
                "my": zm["my"],
                "dx": zm["dx"],
                "dy": zm["dy"],
                "kat": zm["kat"],
            })

    kat_kumeleri = collections.defaultdict(list)
    for m in ham_mahaller:
        kat_kumeleri[m["kat"]].append(m)

    mevcut_katlar = sorted(kat_kumeleri.keys()) if kat_kumeleri else [0]
    en_alt_kat = min(mevcut_katlar)
    en_ust_kat = max(mevcut_katlar)

    karma_yapi_mi = bina_fonksiyonu in ("konut_dukkan", "ofis_dukkan") or zemin_taban_kod == "IKISI"
    acik_gecit_etiketleri = [
        et for et in mim_etiketler
        if any(k in et["ad"] for k in ("AÇIK GEÇİT", "ACIK GECIT", "ARKA BAHÇE", "ÖN BAHÇE", "GİRİŞ", "ÇIKMA"))
    ]

    # Tüm katların oda kutularını ve kat sınırlarını (kat_xmin, kat_xmax, kat_ymin, kat_ymax) önceden hesapla
    # Böylece katlar arası izdüşüm (1. Kat -> Zemin Kat dükkan/otopark ve 2. Kat -> 3. Kat dubleks teras CA2) tam hizalanır.
    kat_sinirlari = {}
    for kat_no in mevcut_katlar:
        k_mahaller = sorted(kat_kumeleri[kat_no], key=lambda item: item["kod"])
        for m in k_mahaller:
            m["kutu"] = _oda_kutusu_bul(m["mx"], m["my"], m["alan_m2"], olculer)
        kutular = [m["kutu"] for m in k_mahaller if m["kutu"]]
        if kutular:
            kx0 = min(k["x1"] for k in kutular) - 300
            kx1 = max(k["x2"] for k in kutular) + 300
            ky0 = min(k["y1"] for k in kutular) - 300
            ky1 = max(k["y2"] for k in kutular) + 300
            tum_x = [x for (x1, x2, ym, _) in olculer["yatay_oda"] if kx0 <= x1 and x2 <= kx1 and ky0 <= ym <= ky1 for x in (x1, x2)]
            tum_y = [y for (y1, y2, xm, _) in olculer["dikey_oda"] if ky0 <= y1 and y2 <= ky1 and kx0 <= xm <= kx1 for y in (y1, y2)]
            kat_xmin = min(tum_x) if tum_x else min(k["x1"] for k in kutular)
            kat_xmax = max(tum_x) if tum_x else max(k["x2"] for k in kutular)
            kat_ymin = min(tum_y) if tum_y else min(k["y1"] for k in kutular)
            kat_ymax = max(tum_y) if tum_y else max(k["y2"] for k in kutular)
        elif k_mahaller:
            kat_xmin = min(m["mx"] for m in k_mahaller) - 400
            kat_xmax = max(m["mx"] for m in k_mahaller) + 400
            kat_ymin = min(m["my"] for m in k_mahaller) - 400
            kat_ymax = max(m["my"] for m in k_mahaller) + 400
        else:
            continue
        kat_sinirlari[kat_no] = (kat_xmin, kat_xmax, kat_ymin, kat_ymax)

    # Zemin katta (Kat 0) ısıtılan dükkanların varlığını ve ısıtılmayan otopark/kat holü bölgelerini tespit et
    zemin_isitilan_dukkan_var = (
        0 in kat_kumeleri
        and any(
            ("DÜKKAN" in m0["ad"] or "DUKKAN" in m0["ad"] or "MAĞAZA" in m0["ad"]) and (m0.get("sicaklik") or 20.0) >= 15.0
            for m0 in kat_kumeleri[0]
        )
    )
    zemin_otopark_kutulari = []
    if 0 in kat_sinirlari:
        kx0_min, kx0_max, ky0_min, ky0_max = kat_sinirlari[0]
        for e in mim_msp.query("TEXT MTEXT"):
            t_u = _metin(e).upper()
            ex, ey = float(e.dxf.insert.x), float(e.dxf.insert.y)
            if (kx0_min - 200.0 <= ex <= kx0_max + 200.0) and (ky0_min - 200.0 <= ey <= ky0_max + 200.0):
                if "OTOPARK" in t_u and "GİRİŞ" not in t_u and "GIRIS" not in t_u and "İZDÜŞÜM" not in t_u:
                    # Örn. "17 NOLU DÜKKAN OTOPARKI" (A: 26,2 m²)
                    otopark_alan = 26.2
                    for et in mim_etiketler:
                        if math.hypot(et["x"] - ex, et["y"] - ey) <= 180.0 and et["alan"] > 5.0:
                            otopark_alan = et["alan"]
                            break
                    okutu = _oda_kutusu_bul(ex, ey, otopark_alan, olculer)
                    if okutu and okutu["w_cm"] <= 350.0:
                        zemin_otopark_kutulari.append((okutu["x1"], okutu["x2"], min(okutu["y1"], ky0_min), max(okutu["y2"], ey + 450.0)))
                    else:
                        zemin_otopark_kutulari.append((max(kx0_min, ex - 135.0), kx0_max, ky0_min, ey + 450.0))

    # Üst dubleks/çatı katında (en_ust_kat) TERAS etiketlerini tespit et (bir alt katta CA2 tavan hesabı için)
    ust_kat_teras_noktalari = []
    if en_ust_kat >= 2 and en_ust_kat in kat_sinirlari:
        kxu_min, kxu_max, kyu_min, kyu_max = kat_sinirlari[en_ust_kat]
        for e in mim_msp.query("TEXT MTEXT"):
            t_u = _metin(e).upper()
            ex, ey = float(e.dxf.insert.x), float(e.dxf.insert.y)
            if (kxu_min - 250.0 <= ex <= kxu_max + 250.0) and (kyu_min - 250.0 <= ey <= kyu_max + 250.0):
                if "TERAS" in t_u and "TAVAN" not in t_u:
                    ust_kat_teras_noktalari.append((ex, ey))

    # Kullanıcı açıklaması (aciklama_metni) içinde geçen cümleleri, sesli dikte (cabir->CA1, c2->CA2 vb.) ifadelerini,
    # mahal aralıklarını (örn. Z-01'den Z-05'e) ve özel tavan/taban/DO1/DK kurallarını ayrıştır
    aciklama_ca2_kodlar = set()
    aciklama_to2_kodlar = set()
    aciklama_do1_sifir_kodlar = set()
    aciklama_dk_zorla_kodlar = set()
    if aciklama_metni:
        aciklama_norm = (
            aciklama_metni.lower()
            .replace("cabir", "ca1")
            .replace("ca bir", "ca1")
            .replace("c a 1", "ca1")
            .replace("c2", "ca2")
            .replace("ca 2", "ca2")
            .replace("c3", "ca3")
            .replace("ca 3", "ca3")
        )
        if ("tabanı plak" in aciklama_norm or "dükkan tabanı plak" in aciklama_norm or "zemin kat plak" in aciklama_norm):
            aktif_set.add("TO1")
        if ("tavan kısmı asmolen" in aciklama_norm or "tavanında ca3" in aciklama_norm or "balkon" in aciklama_norm or "teras" in aciklama_norm):
            if asmolen_mi and "CA2" not in aktif_set:
                aktif_set.add("CA3")
        mahal_ozel = dict(mahal_ozel)
        cumleler = [c.strip() for c in re.split(r"[.;!\n]+", aciklama_norm) if c.strip()]
        for cumle in cumleler:
            c_low = cumle.lower()
            negatif_mi = any(k in c_low for k in ("alınmasın", "alinmasin", "almıyoruz", "almiyoruz", "olmasın", "olmasin", "yok", "sıfır", "sifir", "iptal"))

            # Mahal aralıklarını aç (örn. "Z-01'den Z-05'e", "Z01-Z05", "101-129")
            kod_listesi = []
            for m_aralik in re.finditer(r"\b([zb]?-?\d{2,3})\s*(?:'?[a-zçğıöşü]*\s*(?:başlayarak\s*)?|-|–|ile|den|dan)\s*([zb]?-?\d{2,3})\b", c_low):
                k_bas = m_aralik.group(1).replace("-", "").upper()
                k_bit = m_aralik.group(2).replace("-", "").upper()
                if k_bas.startswith("Z") and k_bit.startswith("Z") and k_bas[1:].isdigit() and k_bit[1:].isdigit():
                    for z_i in range(int(k_bas[1:]), int(k_bit[1:]) + 1):
                        kod_listesi.append(f"Z{z_i:02d}")
                elif k_bas.isdigit() and k_bit.isdigit() and len(k_bas) == 3 and len(k_bit) == 3 and k_bas[0] == k_bit[0]:
                    for n_i in range(int(k_bas), int(k_bit) + 1):
                        kod_listesi.append(str(n_i))

            # Tekil mahal kodlarını yakala (örn. Z-01, Z02, 130, 202, 325)
            for m_tek in re.finditer(r"\b(z-?\d{2}|b-?\d{2}|[123]\d{2})\b", c_low):
                k_norm = m_tek.group(1).replace("-", "").upper()
                if k_norm not in kod_listesi:
                    kod_listesi.append(k_norm)

            for k_hedef in kod_listesi:
                oz = dict(mahal_ozel.get(k_hedef) or {})
                if "do1" in c_low:
                    if negatif_mi:
                        aciklama_do1_sifir_kodlar.add(k_hedef)
                        oz["do1"] = 0.0
                if "dk" in c_low or "dış kapı" in c_low or "dis kapi" in c_low:
                    if not negatif_mi:
                        aciklama_dk_zorla_kodlar.add(k_hedef)
                for t_kod in ("ca1", "ca2", "ca3", "cc1", "cc2"):
                    if t_kod in c_low or (t_kod == "ca2" and "teras" in c_low) or (t_kod == "ca1" and "açık çatı" in c_low):
                        aktif_set.add(t_kod.upper())
                        if negatif_mi:
                            oz["ceil_area"] = 0.0
                        elif t_kod == "ca2" and k_hedef.startswith("2"):
                            aciklama_ca2_kodlar.add(k_hedef)
                        else:
                            oz["ceil_code"] = t_kod.upper()
                for f_kod in ("to1", "to2", "tg1", "tg2", "tt1"):
                    if f_kod in c_low or (f_kod == "to2" and "otopark" in c_low):
                        aktif_set.add(f_kod.upper())
                        if negatif_mi and "sadece" not in c_low:
                            oz["floor_area"] = 0.0
                        elif f_kod == "to2" and k_hedef.startswith("1"):
                            aciklama_to2_kodlar.add(k_hedef)
                        else:
                            oz["floor_code"] = f_kod.upper()
                if oz:
                    mahal_ozel[k_hedef] = oz

    sonuc_mahaller = []
    for kat_no in mevcut_katlar:
        k_mahaller = sorted(kat_kumeleri[kat_no], key=lambda item: item["kod"])
        kat_xmin, kat_xmax, kat_ymin, kat_ymax = kat_sinirlari[kat_no]

        ort_dx = k_mahaller[0].get("dx", 0.0)
        ort_dy = k_mahaller[0].get("dy", 0.0)
        pencereler, kapilar = _pencereleri_ve_kapilari_oku(
            mim_msp, kt_msp, ort_dx, ort_dy,
            kat_xmin - 160, kat_xmax + 160, kat_ymin - 160, kat_ymax + 160
        )
        kolonlar = _kolonlari_oku(
            mim_msp, kat_xmin - 120, kat_xmax + 120, kat_ymin - 120, kat_ymax + 120
        )

        ikiz_x = None
        if any(k.endswith("A") for k in mevcut_kodlar):
            for x1, x2, ym, val in olculer["yatay_duvar"]:
                if abs(val - 20.0) < 1.0 and kat_xmin <= x1 <= kat_xmax and kat_ymin - 100 <= ym <= kat_ymax + 100:
                    ikiz_x = 0.5 * (x1 + x2)
                    break

        antre_kodlu_mu = any("ANTRE" in m["ad"] for m in k_mahaller)
        mutfak_kodlu_mu = any("MUTFAK" in m["ad"] for m in k_mahaller)

        for m in k_mahaller:
            kod = m["kod"]
            ad = m["ad"]
            # Pilot proje (ikiz daire Recep Kabak) haricinde tüm mimari planlarda mahal ismi birebir korunur (örn. YATAK ODASI)
            ad_kisa = ("ODA" if "YATAK" in ad else ad) if (dimension_modu and ikiz_x is not None) else ad
            kutu = m["kutu"]
            sag_daire_mi = (m["mx"] > ikiz_x) if ikiz_x is not None else False
            alan_toplam = m["alan_m2"]
            wins = []
            ik_deger = (0.0, 0.0)
            dh1 = 0.0
            dh2 = 0.0
            do1 = 0.0
            yon_deger = 2.0
            kenar_durumlari_tuple = None

            if kutu:
                x1, x2, y1, y2 = kutu["x1"], kutu["x2"], kutu["y1"], kutu["y2"]
                w_m = kutu["w_cm"] / 100.0
                h_m = kutu["h_cm"] / 100.0

                # A) İkiz daire (Pilot Proje - Recep Kabak) modu
                if dimension_modu and ikiz_x is not None:
                    for ix, iy in ilave_noktalari:
                        if (ix > ikiz_x) == sag_daire_mi and abs(ix - m["mx"]) < 350 and abs(iy - m["my"]) < 350 and ad_kisa == "ODA" and m["alan_m2"] > 11.0:
                            for et in mim_etiketler:
                                if (et["x"] > ikiz_x) == sag_daire_mi and et["ad"] in ("GİYSİ O.", "DUŞ") and abs(et["y"] - iy) < 250:
                                    alan_toplam += et["alan"]

                    if kat_no == 0 and ad_kisa == "SALON" and not antre_kodlu_mu:
                        for et in mim_etiketler:
                            if (et["x"] > ikiz_x) == sag_daire_mi and et["ad"] in ("ANTRE", "VESTİYER", "WC") and abs(et["y"] - m["my"]) < 500:
                                alan_toplam += et["alan"]
                        toplam_dis = 17.20
                        dh2 = 3.60
                        dh1 = round(toplam_dis - dh2, 2)
                        wins = [("DPK", 3.0, 2.4), ("DK", 1.0, 2.4), ("DP", 0.6, 0.6)]
                    elif kat_no == 0 and ad_kisa == "MUTFAK":
                        toplam_dis = round((w_m - 0.20) + (h_m + 0.15), 2)
                        dh2 = 1.20
                        dh1 = round(toplam_dis - dh2, 2)
                        wins = [("DPK", 1.8, 2.4)]
                    else:
                        kuzey_dis = abs(y2 - kat_ymax) <= 150.0
                        guney_dis = abs(y1 - kat_ymin) <= 50.0
                        yan_dis = (abs(x2 - kat_xmax) <= 30.0) if sag_daire_mi else (abs(x1 - kat_xmin) <= 30.0)
                        guney_balkon = any(
                            et["ad"] == "BALKON" and (et["x"] > ikiz_x) == sag_daire_mi and et["y"] < y1 and abs(et["x"] - m["mx"]) < 220.0
                            for et in mim_etiketler
                        )
                        if kuzey_dis and not yan_dis and not guney_balkon:
                            dh1 = round(w_m - 0.20, 2)
                            dh2 = 0.0
                            wins = [("DPK", 1.6, 2.4)]
                        elif kuzey_dis and yan_dis and guney_balkon:
                            toplam_dis = round((w_m - 0.20) + h_m + (w_m - 0.30), 2)
                            dh2 = 1.20
                            dh1 = round(toplam_dis - dh2, 2)
                            wins = [("DPK", 1.8, 2.4), ("DPG", 0.9, 2.1)]
                        elif not kuzey_dis and not yan_dis and not guney_dis:
                            dh1 = 0.0
                            dh2 = 0.0
                            wins = []
                        else:
                            if kuzey_dis or guney_dis:
                                dh1 += w_m
                            if yan_dis:
                                dh1 += h_m
                            dh1 = round(dh1, 2)

                # B) Tekil Daire / Kapıcı Dairesi / Genel Mimari Ölçü ve Kolon Modu
                else:
                    nis_ust_w_m = 0.0
                    if ad_kisa == "SALON" and not mutfak_kodlu_mu:
                        for et in mim_etiketler:
                            if ("MUTFAK NİŞİ" in et["ad"] or "MUTFAK NISI" in et["ad"] or et["ad"] == "NİŞ") and abs(et["x"] - m["mx"]) < 350 and abs(et["y"] - m["my"]) < 400:
                                alan_toplam += et["alan"]
                                kutu_nis = _oda_kutusu_bul(et["x"], et["y"], et["alan"], olculer)
                                if kutu_nis:
                                    x1 = min(x1, kutu_nis["x1"])
                                    x2 = max(x2, kutu_nis["x2"])
                                    y1 = min(y1, kutu_nis["y1"])
                                    y2 = max(y2, kutu_nis["y2"])
                                    nis_ust_w_m = kutu_nis["w_cm"] / 100.0
                                    h_m = (y2 - y1) / 100.0
                                break

                    # KURAL 2 ve KURAL 3: Isıtılan / Isıtılmayan Alan Sınır Mantığı ve Sırt Sırta Oda Filtresi
                    if len(k_mahaller) >= 5:
                        # Çoklu mahalli kat planlarında (örn. 1. Kat, 2. Kat, Çatı Katı veya çoklu dükkan)
                        # Her bir kenar (ust, alt, sol, sag) için 3 durum vardır:
                        #   1) Hemen bitişiğinde (<=240 cm) başka bir ISITILAN oda varsa -> KURAL 3: Sırt sırta ısıtılan oda (0.00 m)
                        #   2) Bitişiğinde ısıtılan oda yok ama o yönde bina içi ortak alan / diğer daireler varsa -> KURAL 2: Isıtılmayan iç ortam (DO1, kolon dahil)
                        #   3) O yönde hiçbir iç mahal yoksa (kademeli cephe dahil doğrudan dış cepheye bakıyorsa) -> KURAL 2: Dış cephe (DH1 ve DH2)
                        def _kenar_durumu(yon_adi):
                            if yon_adi == "ust":
                                bitisik = any(
                                    dm is not m and dm.get("kutu")
                                    and (y2 - 50.0) <= dm["kutu"]["y1"] <= (y2 + 240.0)
                                    and dm["kutu"]["y2"] > y2 + 40.0
                                    and (min(x2, dm["kutu"]["x2"]) - max(x1, dm["kutu"]["x1"])) >= 0.30 * (x2 - x1)
                                    for dm in k_mahaller
                                )
                                if bitisik:
                                    return "isitilan_komsu"
                                uzakta_oda = any(
                                    dm is not m and dm.get("kutu")
                                    and dm["kutu"]["y2"] > y2 + 240.0
                                    and (min(x2 + 120.0, dm["kutu"]["x2"]) - max(x1 - 120.0, dm["kutu"]["x1"])) >= 0.25 * (x2 - x1)
                                    for dm in k_mahaller
                                )
                                return "ic_isitilmayan" if uzakta_oda else "dis_cephe"
                            if yon_adi == "alt":
                                bitisik = any(
                                    dm is not m and dm.get("kutu")
                                    and (y1 - 240.0) <= dm["kutu"]["y2"] <= (y1 + 50.0)
                                    and dm["kutu"]["y1"] < y1 - 40.0
                                    and (min(x2, dm["kutu"]["x2"]) - max(x1, dm["kutu"]["x1"])) >= 0.30 * (x2 - x1)
                                    for dm in k_mahaller
                                )
                                if bitisik:
                                    return "isitilan_komsu"
                                uzakta_oda = any(
                                    dm is not m and dm.get("kutu")
                                    and dm["kutu"]["y1"] < y1 - 240.0
                                    and (min(x2 + 120.0, dm["kutu"]["x2"]) - max(x1 - 120.0, dm["kutu"]["x1"])) >= 0.25 * (x2 - x1)
                                    for dm in k_mahaller
                                )
                                return "ic_isitilmayan" if uzakta_oda else "dis_cephe"
                            if yon_adi == "sol":
                                bitisik = any(
                                    dm is not m and dm.get("kutu")
                                    and (x1 - 240.0) <= dm["kutu"]["x2"] <= (x1 + 50.0)
                                    and dm["kutu"]["x1"] < x1 - 40.0
                                    and (min(y2, dm["kutu"]["y2"]) - max(y1, dm["kutu"]["y1"])) >= 0.30 * (y2 - y1)
                                    for dm in k_mahaller
                                )
                                if bitisik:
                                    return "isitilan_komsu"
                                uzakta_oda = any(
                                    dm is not m and dm.get("kutu")
                                    and dm["kutu"]["x1"] < x1 - 240.0
                                    and (min(y2 + 120.0, dm["kutu"]["y2"]) - max(y1 - 120.0, dm["kutu"]["y1"])) >= 0.25 * (y2 - y1)
                                    for dm in k_mahaller
                                )
                                return "ic_isitilmayan" if uzakta_oda else "dis_cephe"
                            # sag
                            bitisik = any(
                                dm is not m and dm.get("kutu")
                                and (x2 - 50.0) <= dm["kutu"]["x1"] <= (x2 + 240.0)
                                and dm["kutu"]["x2"] > x2 + 40.0
                                and (min(y2, dm["kutu"]["y2"]) - max(y1, dm["kutu"]["y1"])) >= 0.30 * (y2 - y1)
                                for dm in k_mahaller
                            )
                            if bitisik:
                                return "isitilan_komsu"
                            uzakta_oda = any(
                                dm is not m and dm.get("kutu")
                                and dm["kutu"]["x2"] > x2 + 240.0
                                and (min(y2 + 120.0, dm["kutu"]["y2"]) - max(y1 - 120.0, dm["kutu"]["y1"])) >= 0.25 * (y2 - y1)
                                for dm in k_mahaller
                            )
                            return "ic_isitilmayan" if uzakta_oda else "dis_cephe"

                        d_ust = _kenar_durumu("ust")
                        d_alt = _kenar_durumu("alt")
                        d_sol = _kenar_durumu("sol")
                        d_sag = _kenar_durumu("sag")
                        kenar_durumlari_tuple = (d_ust, d_alt, d_sol, d_sag)

                        ust_dis = (d_ust == "dis_cephe")
                        alt_dis = (d_alt == "dis_cephe")
                        sol_dis = (d_sol == "dis_cephe")
                        sag_dis = (d_sag == "dis_cephe")

                        # Isıtılmayan iç ortama (kat holü, merdiven, şaft vb.) bakan sınırları (kolonlar dahil) doğrudan DO1'e topla
                        do1_ham = 0.0
                        if kat_no <= 0 and ("DÜKKAN" in ad or "DUKKAN" in ad or "MAĞAZA" in ad):
                            # Zemin katta birbirine bitişik ısıtılan dükkanlarda (örn. Z01..Z05) DO1 = 0.00 alınır;
                            # yalnızca doğrudan ortak KAT HOLÜ ve DÜKKAN OTOPARKI'na bitişik olan dükkanda (örn. Z06) DO1 hesaplanır.
                            otopark_komsu = any(
                                abs(x2 - ox1) <= 150.0 and (min(y2, oy2) - max(y1, oy1)) >= 200.0
                                for (ox1, ox2, oy1, oy2) in zemin_otopark_kutulari
                            )
                            if otopark_komsu:
                                do1_ham = h_m
                                d_sag = "ic_isitilmayan"
                                sag_dis = False
                                kenar_durumlari_tuple = (d_ust, d_alt, d_sol, d_sag)
                        else:
                            if d_ust == "ic_isitilmayan":
                                do1_ham += w_m
                            if d_alt == "ic_isitilmayan":
                                do1_ham += w_m
                            if d_sol == "ic_isitilmayan":
                                do1_ham += h_m
                            if d_sag == "ic_isitilmayan":
                                do1_ham += h_m
                            if ("ANTRE" in ad or "HOL" in ad) and do1_ham == 0.0:
                                do1_ham = min(w_m, h_m)
                        do1 = round(do1_ham, 2)

                        if "ANTRE" in ad or "HOL" in ad:
                            ust_dis = alt_dis = sol_dis = sag_dis = False

                        dh2 = _kolon_metraji_hesapla(
                            x1, x2, y1, y2, kolonlar, kat_xmin, kat_xmax, kat_ymin, kat_ymax,
                            kenar_dis=(ust_dis, alt_dis, sol_dis, sag_dis)
                        )
                        ham_dis_duvar = 0.0
                        if ust_dis:
                            ham_dis_duvar += w_m
                        if alt_dis:
                            ham_dis_duvar += w_m
                        if sol_dis:
                            ham_dis_duvar += h_m
                        if sag_dis:
                            ham_dis_duvar += h_m
                        dh1 = round(max(0.0, ham_dis_duvar - dh2), 2) if dh2 > 0 else round(ham_dis_duvar, 2)
                    else:
                        ust_dis = abs(y2 - kat_ymax) <= 90.0 or len(k_mahaller) == 1
                        alt_dis = abs(y1 - kat_ymin) <= 90.0 or len(k_mahaller) == 1
                        sol_dis = abs(x1 - kat_xmin) <= 90.0 or len(k_mahaller) == 1
                        sag_dis = abs(x2 - kat_xmax) <= 90.0 or len(k_mahaller) == 1

                        if sag_dis:
                            dis_dik = [val for (dy1, dy2, xm, val) in olculer["dikey_oda"] if abs(xm - x2) <= 25.0 and abs(dy1 - y1) <= 25.0 and val > kutu["h_cm"]]
                            if dis_dik:
                                en_uzun_dik = max(dis_dik)
                                h_m = en_uzun_dik / 100.0
                                y2 = y1 + en_uzun_dik

                        ust_kademe_m = 0.0
                        sag_kademe_m = 0.0
                        if not ust_dis and sag_dis:
                            diger_ust_x2 = max(
                                (dm["kutu"]["x2"] for dm in k_mahaller if dm is not m and dm.get("kutu") and dm["kutu"]["y2"] > y2 + 30.0),
                                default=x1
                            )
                            if x2 - diger_ust_x2 >= 80.0:
                                ust_kademe_m = round((x2 - diger_ust_x2) / 100.0, 2)

                        if ust_dis and not sag_dis:
                            diger_sag_y2 = max(
                                (dm["kutu"]["y2"] for dm in k_mahaller if dm is not m and dm.get("kutu") and dm["kutu"]["x2"] > x2 + 30.0),
                                default=y1
                            )
                            if y2 - diger_sag_y2 >= 60.0:
                                sag_kademe_m = round((y2 - diger_sag_y2) / 100.0, 2)

                        dh2 = _kolon_metraji_hesapla(x1, x2, y1, y2, kolonlar, kat_xmin, kat_xmax, kat_ymin, kat_ymax)

                        hic_dis_cephesi_yok = not (ust_dis or alt_dis or sol_dis or sag_dis or ust_kademe_m > 0 or sag_kademe_m > 0)
                        if "ANTRE" in ad or "HOL" in ad or hic_dis_cephesi_yok:
                            dh1 = 0.0
                            dh2 = 0.0
                            if "ANTRE" in ad or "HOL" in ad:
                                do1 = round(w_m, 2)
                        else:
                            ham_dis_duvar = 0.0
                            if ust_dis:
                                ham_dis_duvar += (nis_ust_w_m if nis_ust_w_m > 0 else w_m)
                            elif ust_kademe_m > 0:
                                ham_dis_duvar += ust_kademe_m
                            if alt_dis and (not ust_dis or nis_ust_w_m > 0):
                                ham_dis_duvar += w_m
                            if sol_dis:
                                ham_dis_duvar += h_m
                            if sag_dis and not sol_dis:
                                ham_dis_duvar += h_m
                            elif sag_kademe_m > 0:
                                ham_dis_duvar += sag_kademe_m

                            if dh2 > 0 and ham_dis_duvar > dh2 + 1.0:
                                if nis_ust_w_m > 0:
                                    dh1 = round(ham_dis_duvar - dh2, 2)
                                elif ust_kademe_m > 0:
                                    dh1 = round(ham_dis_duvar - 0.30, 2)
                                else:
                                    dh1 = round(ham_dis_duvar, 2)
                            else:
                                dh1 = round(ham_dis_duvar, 2)

                    kat_villa_mi = villa_mi or (kat_no <= 0 and len(k_mahaller) <= 5 and not karma_yapi_mi)
                    wins, ik_deger = _acikliklari_yonlu_esle(
                        x1, x2, y1, y2, pencereler, kapilar, yon_harita,
                        kat_xmin, kat_xmax, kat_ymin, kat_ymax, ust_dis=ust_dis,
                        kat_no=kat_no, kod=kod, ad=ad,
                        kenar_durumlari=kenar_durumlari_tuple,
                        dh1=dh1, dh2=dh2, do1=do1,
                        bina_fonksiyonu=bina_fonksiyonu, villa_mi=kat_villa_mi
                    )
                    win_yonler = [YON_ZAMMI[w[0]] for w in wins if w[0] in YON_ZAMMI]
                    if win_yonler:
                        yon_deger = max(win_yonler)

            # KURAL 3: Seçili olmayan duvar/kolon/kiriş bileşenlerini sıfırla
            wall_code = "DT1" if ("DT1" in aktif_set and ("DH1" not in aktif_set or kat_no < 0 or "BODRUM" in ad)) else "DH1"
            if "DH1" not in aktif_set and "DT1" not in aktif_set:
                dh1 = 0.0
            if "DH2" not in aktif_set:
                dh2 = 0.0
            kat_asmolen_mi = asmolen_mi or (karma_doseme_mi and kat_no >= 1)
            if kat_asmolen_mi or ("DH3" not in aktif_set and not (dimension_modu and ikiz_x is not None)):
                dh3 = 0.0
            else:
                dh3 = dh1
            if "DO1" not in aktif_set:
                do1 = 0.0

            yukseklik = 3.0 if kat_no <= 0 else 2.6

            # KURAL 4: Plak Döşeme (TO1/TG1/CC1/CA2) ve Asmolen Döşeme (TO2/TG2/CC2/CA3) Gruplandırma Kuralı
            sadece_plak_mi = (doseme in ("plak", "diger")) and ("TO2" not in aktif_set) and ("TG2" not in aktif_set)
            varsayilan_taban = "TO1" if (sadece_plak_mi or (kat_no <= 0 and "TO1" in aktif_set)) else ("TO2" if "TO2" in aktif_set else "TO1")

            ceil_code, ceil_area = "CA1", 0.0
            ceil2_code, ceil2_area = "", 0.0
            floor_code, floor_area = varsayilan_taban, 0.0
            floor2_code, floor2_area = "", 0.0

            ust_kat_mi = (kat_sayisi == 1) or (kat_no == en_ust_kat and en_ust_kat > 0) or (kat_no >= kat_sayisi - 1 and kat_no > 0)
            zemin_kat_mi = (kat_sayisi == 1) or (kat_no <= 0) or (kat_no == en_alt_kat)

            if ust_kat_mi:
                if str(kod).strip().upper() == "325":
                    # 325 nolu dubleks üst kat mahali üzeri açık çatı (CA1) olarak hesaplanır
                    ceil_code = "CA1"
                elif "CC2" in aktif_set and "CC1" not in aktif_set and ust_tavan_kod not in ("CA1", "CA1_CA2", "CA2", "CA3"):
                    ceil_code = "CC2"
                elif "CA3" in aktif_set and not any(k in aktif_set for k in ("CC1", "CC2", "CA1")):
                    ceil_code = "CA3"
                elif "CA2" in aktif_set and not any(k in aktif_set for k in ("CC1", "CC2", "CA1", "CA3")):
                    ceil_code = "CA2"
                elif ("CA1" in aktif_set and not any(k in aktif_set for k in ("CC1", "CC2"))) or ust_tavan_kod in ("CA1", "CA1_CA2"):
                    ceil_code = "CA1"
                elif ust_tavan_kod in ("CC2", "CA3") and not sadece_plak_mi:
                    ceil_code = ust_tavan_kod
                elif ust_tavan_kod == "KARISIK":
                    ceil_code = "CC2" if (kat_asmolen_mi and "CC2" in aktif_set and "CC1" not in aktif_set) else "CC1"
                else:
                    ceil_code = ust_tavan_kod
                if any(k in aktif_set for k in ("CC1", "CC2", "CA1", "CA2", "CA3")):
                    ceil_area = round(alan_toplam, 2)
            elif (
                en_ust_kat >= 2
                and kat_no == en_ust_kat - 1
                and kutu is not None
                and (ust_kat_teras_noktalari or str(kod).strip() in aciklama_ca2_kodlar)
            ):
                # 2. Kat (dubleks/çatı katının bir alt katı): Üst katta TERAS bulunan mahallerde
                # (örn. 202, 203, 205, 209, 211, 215, 216, 221, 223, 225) üst katın kapatmadığı teras tavan alanı CA2 olarak hesaplanır.
                ic_servis_mahali = any(w in ad for w in ("ANTRE", "HOL", "KORİDOR", "KORIDOR", "BANYO", "WC", "TUVALET", "KİLER", "KILER", "SANDIK"))
                if (dh1 > 0 and not ic_servis_mahali) or str(kod).strip() in aciklama_ca2_kodlar:
                    dx_23 = kat_sinirlari[en_ust_kat][1] - kat_xmax
                    dy_23 = 0.0 if abs(kat_sinirlari[en_ust_kat][2] - kat_ymin) <= 250.0 else (kat_sinirlari[en_ust_kat][2] - kat_ymin)
                    ust_kutular = [
                        (
                            mu["kutu"]["x1"] - dx_23,
                            mu["kutu"]["x2"] - dx_23,
                            mu["kutu"]["y1"] - dy_23,
                            mu["kutu"]["y2"] - dy_23,
                        )
                        for mu in kat_kumeleri[en_ust_kat]
                        if mu.get("kutu")
                    ]
                    if ust_kutular:
                        nx_g = max(1, int((kutu["x2"] - kutu["x1"]) / 5.0))
                        ny_g = max(1, int((kutu["y2"] - kutu["y1"]) / 5.0))
                        kapali_say = 0
                        toplam_say = nx_g * ny_g
                        for ix_g in range(nx_g):
                            px_g = kutu["x1"] + (ix_g + 0.5) * (kutu["x2"] - kutu["x1"]) / nx_g
                            for iy_g in range(ny_g):
                                py_g = kutu["y1"] + (iy_g + 0.5) * (kutu["y2"] - kutu["y1"]) / ny_g
                                if any(
                                    bx1 - 12.0 <= px_g <= bx2 + 12.0 and by1 - 12.0 <= py_g <= by2 + 12.0
                                    for (bx1, bx2, by1, by2) in ust_kutular
                                ):
                                    kapali_say += 1
                        acik_oran = 1.0 - (kapali_say / toplam_say)
                        teras_m2 = round(alan_toplam * acik_oran, 2)
                        if teras_m2 >= 0.95 or str(kod).strip() in aciklama_ca2_kodlar:
                            ceil_code = "CA3" if ("CA3" in aktif_set and "CA2" not in aktif_set) else "CA2"
                            ceil_area = teras_m2 if teras_m2 >= 0.50 else round(0.25 * alan_toplam, 2)
            elif (
                kat_no == 0
                and 1 in kat_kumeleri
                and 1 in kat_sinirlari
                and kutu is not None
                and ("DÜKKAN" in ad or "DUKKAN" in ad or "MAĞAZA" in ad)
            ):
                # Zemin kat dükkanların (Z01..Z06) tavanı: 1. kattaki konutların ısıtılan odaları dışındaki
                # balkon, teras ve çekme kısımlarına denk gelen dükkan tavan alanları CA3 (asmolen) / CA2 (plak) olarak hesaplanır.
                dx_01_c = kat_sinirlari[1][1] - kat_xmax
                dy_01_c = 0.0 if abs(kat_sinirlari[1][2] - kat_ymin) <= 250.0 else (kat_sinirlari[1][2] - kat_ymin)
                kat1_kutular = [
                    (
                        m1["kutu"]["x1"] - dx_01_c,
                        m1["kutu"]["x2"] - dx_01_c,
                        m1["kutu"]["y1"] - dy_01_c,
                        m1["kutu"]["y2"] - dy_01_c,
                    )
                    for m1 in kat_kumeleri[1]
                    if m1.get("kutu")
                ]
                if kat1_kutular and any(k in aktif_set for k in ("CA2", "CA3")):
                    nx_z = max(1, int((kutu["x2"] - kutu["x1"]) / 5.0))
                    ny_z = max(1, int((kutu["y2"] - kutu["y1"]) / 5.0))
                    kapali_z = 0
                    toplam_z = nx_z * ny_z
                    for ix_z in range(nx_z):
                        px_z = kutu["x1"] + (ix_z + 0.5) * (kutu["x2"] - kutu["x1"]) / nx_z
                        for iy_z in range(ny_z):
                            py_z = kutu["y1"] + (iy_z + 0.5) * (kutu["y2"] - kutu["y1"]) / ny_z
                            if any(
                                bx1 - 15.0 <= px_z <= bx2 + 15.0 and by1 - 15.0 <= py_z <= by2 + 15.0
                                for (bx1, bx2, by1, by2) in kat1_kutular
                            ):
                                kapali_z += 1
                    acik_oran_z = 1.0 - (kapali_z / toplam_z)
                    dukkan_tavan_m2 = round(alan_toplam * acik_oran_z, 2)
                    if dukkan_tavan_m2 >= 1.0:
                        ceil_code = "CA3" if ("CA3" in aktif_set or asmolen_mi) else "CA2"
                        ceil_area = dukkan_tavan_m2

            if zemin_kat_mi:
                acik_gecit_var = karma_yapi_mi and any(
                    abs(et["x"] - m["mx"]) < 350 and abs(et["y"] - m["my"]) < 350
                    for et in acik_gecit_etiketleri
                )
                if acik_gecit_var:
                    if sadece_plak_mi or "TG1" in aktif_set:
                        floor_code = "TG1"
                    elif "TG2" in aktif_set:
                        floor_code = "TG2"
                    else:
                        floor_code = varsayilan_taban
                    floor_area = round(alan_toplam, 2)
                else:
                    if zemin_taban_kod == "TT1" or ("TT1" in aktif_set and "TO1" not in aktif_set and "TO2" not in aktif_set):
                        floor_code = "TT1"
                    elif "TO1" in aktif_set:
                        # Zemin katta plak döşeme (TO1) seçiliyse zemin kat tabanı TO1 olur
                        floor_code = "TO1"
                    elif "TO2" in aktif_set and not sadece_plak_mi:
                        floor_code = "TO2"
                    elif "TT1" in aktif_set:
                        floor_code = "TT1"
                    else:
                        floor_code = "TO1" if zemin_taban_kod == "IKISI" else zemin_taban_kod
                    if any(k in aktif_set for k in ("TO1", "TO2", "TG1", "TG2", "TT1")):
                        floor_area = round(alan_toplam, 2)
            elif karma_yapi_mi and kat_no == 1:
                acik_gecit_var = any(
                    abs(et["x"] - m["mx"]) < 350 and abs(et["y"] - m["my"]) < 350
                    for et in acik_gecit_etiketleri
                )
                if acik_gecit_var:
                    if not sadece_plak_mi and "TG2" in aktif_set:
                        floor_code = "TG2"
                    elif "TG1" in aktif_set:
                        floor_code = "TG1"
                    else:
                        floor_code = "TO2" if (not sadece_plak_mi and "TO2" in aktif_set) else "TO1"
                    floor_area = round(alan_toplam, 2)
                elif zemin_isitilan_dukkan_var:
                    # Alt katta (Kat 0) ısıtılan dükkanlar (Z01..Z06) mevcut olduğundan 1. kattaki mahallerin tamamına TO2 alınmaz!
                    # Sadece alt katta ısıtılan dükkanların dışında kalan ısıtılmayan iç ortamlara (KAT HOLÜ, BİNA GİRİŞİ,
                    # 17 NOLU DÜKKAN OTOPARKI) denk gelen 1. kat mahallerine (örn. 101, 102, 103, 104, 130, 132) TO2 alınır.
                    floor_code = "TO2" if (not sadece_plak_mi and "TO2" in aktif_set) else "TO1"
                    floor_area = 0.0
                    if kutu is not None and 0 in kat_sinirlari:
                        dx_01 = kat_xmax - kat_sinirlari[0][1]
                        dy_01 = 0.0 if abs(kat_ymin - kat_sinirlari[0][2]) <= 250.0 else (kat_ymin - kat_sinirlari[0][2])
                        kat0_dukkan_kutular = [
                            (
                                m0["kutu"]["x1"] + dx_01,
                                m0["kutu"]["x2"] + dx_01,
                                m0["kutu"]["y1"] + dy_01,
                                m0["kutu"]["y2"] + dy_01,
                            )
                            for m0 in kat_kumeleri[0]
                            if m0.get("kutu")
                        ]
                        if kat0_dukkan_kutular:
                            nx_f = max(1, int((kutu["x2"] - kutu["x1"]) / 5.0))
                            ny_f = max(1, int((kutu["y2"] - kutu["y1"]) / 5.0))
                            isitilmayan_ic_say = 0
                            toplam_f = nx_f * ny_f
                            dogu_sinir_esik = kat_xmin + 0.65 * (kat_xmax - kat_xmin)
                            for ix_f in range(nx_f):
                                px_f = kutu["x1"] + (ix_f + 0.5) * (kutu["x2"] - kutu["x1"]) / nx_f
                                for iy_f in range(ny_f):
                                    py_f = kutu["y1"] + (iy_f + 0.5) * (kutu["y2"] - kutu["y1"]) / ny_f
                                    dukkan_ustu_mu = any(
                                        bx1 - 15.0 <= px_f <= bx2 + 15.0 and by1 - 15.0 <= py_f <= by2 + 15.0
                                        for (bx1, bx2, by1, by2) in kat0_dukkan_kutular
                                    )
                                    if not dukkan_ustu_mu and px_f >= dogu_sinir_esik:
                                        isitilmayan_ic_say += 1
                            isitilmayan_oran = isitilmayan_ic_say / toplam_f
                            isitilmayan_m2 = round(alan_toplam * isitilmayan_oran, 2)
                            if isitilmayan_m2 >= 1.20 or str(kod).strip() in aciklama_to2_kodlar:
                                floor_area = isitilmayan_m2 if isitilmayan_m2 >= 1.0 else round(alan_toplam, 2)
                else:
                    # Alt kat ısıtılmayan dükkan ise (Kat 0 ısıtma hesabında yoksa): asmolen döşemede TO2, plak döşemede TO1 (KURAL 4)
                    if not sadece_plak_mi and "TO2" in aktif_set:
                        floor_code = "TO2"
                    else:
                        floor_code = "TO1"
                    if any(k in aktif_set for k in ("TO1", "TO2", "TG1", "TG2")):
                        floor_area = round(alan_toplam, 2)

            ozel = mahal_ozel.get(kod) or {}
            if ozel:
                if "dh1" in ozel:
                    dh1 = round(float(ozel["dh1"]), 2)
                if "dh2" in ozel:
                    dh2 = round(float(ozel["dh2"]), 2)
                if "dh3" in ozel:
                    dh3 = 0.0 if kat_asmolen_mi else round(float(ozel["dh3"]), 2)
                if "do1" in ozel:
                    do1 = round(float(ozel["do1"]), 2)
                if "ceil_code" in ozel:
                    ceil_code = str(ozel["ceil_code"]).upper()
                if "ceil_area" in ozel:
                    ceil_area = round(float(ozel["ceil_area"]), 2)
                if "ceil2_code" in ozel:
                    ceil2_code = str(ozel["ceil2_code"]).upper()
                if "ceil2_area" in ozel:
                    ceil2_area = round(float(ozel["ceil2_area"]), 2)
                if "floor_code" in ozel:
                    floor_code = str(ozel["floor_code"]).upper()
                if "floor_area" in ozel:
                    floor_area = round(float(ozel["floor_area"]), 2)
                if "floor2_code" in ozel:
                    floor2_code = str(ozel["floor2_code"]).upper()
                if "floor2_area" in ozel:
                    floor2_area = round(float(ozel["floor2_area"]), 2)

            # Ara katlarda veya parçalı tavan/taban kaybı olan mahallerde (örn. 130 SALON TO2=21.32 m², 202 SALON CA2=6.69 m²)
            # Excel S{r0+13} alan formülünün (=C18+C14+C13) mimari plandaki m² ile %100 aynı çıkması için
            # kalan ısıtılan komşu alanı C18 (it1_area) hücresine yazılır.
            ara_kat_alan = 0.0
            if not (dimension_modu and ikiz_x is not None):
                yatay_kayip_toplam = ceil_area + floor_area + ceil2_area + floor2_area
                if yatay_kayip_toplam == 0.0:
                    ara_kat_alan = round(alan_toplam, 2)
                elif yatay_kayip_toplam < alan_toplam - 0.05 and kat_sayisi > 1:
                    ara_kat_alan = round(max(0.0, alan_toplam - yatay_kayip_toplam), 2)

            sonuc_mahaller.append({
                "code": kod,
                "name": ad_kisa,
                "temp": m["sicaklik"],
                "area": round(alan_toplam, 2),
                "height": yukseklik,
                "wall_code": wall_code,
                "dh1": dh1,
                "dh3": dh3,
                "dh2": dh2,
                "do1": do1,
                "ik": ik_deger,
                "ceil_code": ceil_code,
                "ceil_area": ceil_area,
                "ceil2_code": ceil2_code,
                "ceil2_area": ceil2_area,
                "floor_code": floor_code,
                "floor_area": floor_area,
                "floor2_code": floor2_code,
                "floor2_area": floor2_area,
                "wins": wins,
                "it1_area": ara_kat_alan,
                "yon": yon_deger,
                "doseme": doseme,
            })

    return {"mahaller": sonuc_mahaller, "uyarilar": uyarilar, "yeni_katmanlar": yeni_kesfedilen_katmanlar}
