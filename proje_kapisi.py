"""
PROJE KAPISI - yanlış Excel üretimini engeller.

Hesaplama motoru şu an YALNIZ doğrulanmış tek proje için (11737 - Sumru, A/B/C Blok) geometri verisi
taşıyor. Duvar uzunluğu (DH1/DO1...) okuyucusu henüz yok. Bu yüzden /hesapla çalışmadan önce
yüklenen kodlu DXF'in mahal listesi (kod, ad, alan, sıcaklık) bilinen projeyle BİREBİR karşılaştırılır.
Eşleşmezse Excel üretilmez ve kullanıcıya nedeni söylenir.
"""
import os

from dwg_excel_otomasyon_calisma import BLOCK_CONFIGS, base_13_rooms

ALAN_TOLERANS = 0.005
_OKUMA_ONBELLEK = {}


def _beklenen_mahaller():
    """Doğrulanmış projenin 52 mahali: {kod: (ad, alan, sıcaklık)}"""
    cfg = BLOCK_CONFIGS["A BLOK"]
    sonuc = {}
    for zemin in (True, False):
        for alt in (False, True):
            for r in base_13_rooms(is_normal_kat=not zemin, is_lower_a=alt, block_cfg=cfg):
                kod = ("Z" if zemin else "N") + f"{r['num']:02d}" + ("A" if alt else "")
                sonuc[kod] = (r["name"], r["area"], r["temp"])
    return sonuc


def _mahal_no_katmani_var_mi(yol):
    """Dev DXF'i ezdxf ile açmadan önce hızlı ham tarama."""
    hedefler = (b"mahal no", b"mahal_no", b"mahal-no", b"mahalno")
    kuyruk = b""
    with open(yol, "rb") as f:
        while True:
            blok = f.read(8 * 1024 * 1024)
            if not blok:
                return False
            veri = (kuyruk + blok).lower()
            if any(h in veri for h in hedefler):
                return True
            kuyruk = blok[-16:]


def _oku(yol):
    anahtar = (yol, os.path.getsize(yol), os.path.getmtime(yol))
    if anahtar not in _OKUMA_ONBELLEK:
        from dxf_okuyucu import mahal_oku  # ezdxf gerektirir
        _OKUMA_ONBELLEK.clear()
        _OKUMA_ONBELLEK[anahtar] = mahal_oku(yol)
    return _OKUMA_ONBELLEK[anahtar]


def karsilastir(okunan_mahaller):
    """(eslesiyor_mu, farklar[]) döner."""
    beklenen = _beklenen_mahaller()
    okunan = {m["kod"]: m for m in okunan_mahaller}
    farklar = []
    for kod, (ad, alan, sic) in beklenen.items():
        m = okunan.get(kod)
        if m is None:
            farklar.append(f"{kod}: çizimde yok")
            continue
        if m["alan_m2"] is None or abs(m["alan_m2"] - alan) > ALAN_TOLERANS:
            farklar.append(f"{kod}: alan çizimde {m['alan_m2']} m², beklenen {alan} m²")
        if m["sicaklik"] != sic:
            farklar.append(f"{kod}: sıcaklık çizimde {m['sicaklik']}°C, beklenen {sic}°C")
        if not (m["ad"] == ad or (ad == "BANYO" and m["ad"] in ("DUŞ", "BANYO"))):
            farklar.append(f"{kod}: ad çizimde {m['ad']!r}, beklenen {ad!r}")
    fazla = sorted(set(okunan) - set(beklenen))
    if fazla:
        farklar.append(f"çizimde bu projede olmayan {len(fazla)} fazla mahal kodu var: {', '.join(fazla[:8])}")
    return (not farklar), farklar


def kontrol(oturum_dizini, test_modu):
    """(izinli_mi, mesaj). Kapı kapatılabilir: PROJE_KAPISI=kapali"""
    if os.environ.get("PROJE_KAPISI", "").strip().lower() in ("kapali", "kapalı", "off", "0"):
        return True, ""

    dosyalar = os.listdir(oturum_dizini) if os.path.isdir(oturum_dizini) else []
    dxfler = [os.path.join(oturum_dizini, d) for d in dosyalar if d.lower().endswith(".dxf")]
    dwgler = [d for d in dosyalar if d.lower().endswith(".dwg")]

    if not dxfler:
        if dwgler:
            return False, ("⚠️ .dwg dosyaları henüz okunamıyor. Lütfen çizimi AutoCAD'de "
                           "DXF olarak kaydedip gönderin.")
        if test_modu:
            return True, ""
        return False, "📂 Önce kodlanmış mimari çizimi (.dxf) gönderin, sonra /hesapla yazın."

    adaylar = [p for p in dxfler if _mahal_no_katmani_var_mi(p)]
    if not adaylar:
        return False, ("⚠️ Yüklenen DXF dosyalarında mahal kodları ('MAHAL NO' katmanı) bulunamadı. "
                       "Mahal kodlarının yazılı olduğu çizimi gönderin.")
    try:
        en_iyi_farklar = None
        son_okunan = []
        for p in adaylar:
            okunan = _oku(p)["mahaller"]
            ok, farklar = karsilastir(okunan)
            if ok:
                return True, ""
            if en_iyi_farklar is None or len(farklar) < len(en_iyi_farklar):
                en_iyi_farklar, son_okunan = farklar, okunan
    except ImportError:
        return False, "⚠️ Sunucuda 'ezdxf' kurulu değil (pip install ezdxf). Yönetici ile iletişime geçin."
    except Exception as e:  # bozuk / okunamayan DXF
        return False, f"⚠️ DXF okunamadı: {type(e).__name__}. Dosyayı yeniden DXF olarak kaydedip gönderin."

    kodlar = sorted(m["kod"] for m in son_okunan)
    okunan_ozet = (f"Çizimde {len(kodlar)} mahal kodu okundu: " + ", ".join(kodlar[:12]) +
                   (" ..." if len(kodlar) > 12 else "")) if kodlar else "Çizimde mahal kodu okunamadı."
    return False, ("⛔ Bu çizim için Excel ÜRETİLMEDİ.\n\n"
                   "Bu sürüm yalnızca doğrulanmış tek projeyi (11737 - Sumru, A/B/C Blok) işleyebiliyor. "
                   "Başka projelerde duvar uzunluğu (DH1/DO1) okuyucusu henüz yok; yanlış sonuç vermemek için "
                   "hesap yapılmıyor.\n\n" + okunan_ozet)
