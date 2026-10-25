import base64
import datetime
import glob
import json
import threading
from email.parser import BytesParser
from email.policy import default as email_default_policy
import os
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Import room/block builder from our verified module
from dwg_excel_otomasyon_calisma import BLOCK_CONFIGS, build_all_floors_for_block
from excel_yazici_v2 import generate_xlsm_pure_python
import proje_kapisi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERI_DIR = os.environ.get("VERI_DIZINI", "").strip() or BASE_DIR
os.makedirs(VERI_DIR, exist_ok=True)
DEFAULT_TEMPLATE = os.path.join(BASE_DIR, "Klima Hesap Tablosu_HAM_YEDEK.xlsm")
SESSIONS_DIR = os.path.join(VERI_DIR, "telegram_oturumlar")
os.makedirs(SESSIONS_DIR, exist_ok=True)

ET.register_namespace("", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

HERKESE_ACIK_TEST_MODU = os.environ.get("HERKESE_ACIK_TEST_MODU", "true").strip().lower() in ("1", "true", "evet")
API_ANAHTARI = os.environ.get("API_ANAHTARI", "").strip()

KULLANICI_DOSYASI = os.path.join(VERI_DIR, "yetkili_kullanicilar.json")
_KULLANICI_KILIDI = threading.Lock()


def _kullanicilari_oku():
    if not os.path.exists(KULLANICI_DOSYASI):
        return {}
    with open(KULLANICI_DOSYASI, "r", encoding="utf-8") as f:
        return json.load(f)


def yetki_kontrol(telegram_id, kredi_dus=False):
    """(izinli_mi, mesaj) döner. kredi_dus=True ise başarılı kontrolde 1 kredi düşer."""
    telegram_id = str(telegram_id or "").strip()
    if HERKESE_ACIK_TEST_MODU:
        return True, ""
    with _KULLANICI_KILIDI:
        try:
            kullanicilar = _kullanicilari_oku()
        except (OSError, ValueError):
            return False, "⚠️ Abonelik listesi okunamadı. Lütfen yöneticiye bildirin."
        kayit = kullanicilar.get(telegram_id)
        yok_mesaji = (f"⛔ Bu bot abonelikle çalışmaktadır.\n🔑 Telegram ID'niz: {telegram_id}\n"
                      "Abonelik için bu numarayı yöneticiye iletin.")
        if not kayit or not kayit.get("aktif"):
            return False, yok_mesaji
        bitis = kayit.get("bitis")
        if bitis and datetime.date.today() > datetime.date.fromisoformat(bitis):
            return False, f"⌛ Aboneliğiniz {bitis} tarihinde sona erdi.\n🔑 Telegram ID'niz: {telegram_id}"
        if int(kayit.get("kalan_kredi", 0)) <= 0:
            return False, "📉 Proje krediniz bitti. Yeni paket için yöneticiyle iletişime geçin."
        if kredi_dus:
            kayit["kalan_kredi"] = int(kayit["kalan_kredi"]) - 1
            with open(KULLANICI_DOSYASI, "w", encoding="utf-8") as f:
                json.dump(kullanicilar, f, ensure_ascii=False, indent=2)
    return True, ""


def generate_xlsm_pure_python(template_path, output_path, block_name, mahals):
    with zipfile.ZipFile(template_path, "r") as zin:
        ss_xml = zin.read("xl/sharedStrings.xml").decode("utf-8")
        s2_xml = zin.read("xl/worksheets/sheet2.xml").decode("utf-8")
        wb_xml = zin.read("xl/workbook.xml").decode("utf-8")

    ss_root = ET.fromstring(ss_xml)
    shared_map = {}
    si_list = ss_root.findall("s:si", NS)
    for idx, si in enumerate(si_list):
        t_el = si.find("s:t", NS)
        if t_el is not None and len(list(si)) == 1 and t_el.text is not None:
            if t_el.text not in shared_map:
                shared_map[t_el.text] = idx

    added_count = 0

    def get_or_add_sst(text_val):
        nonlocal added_count
        text_val = str(text_val)
        if text_val in shared_map:
            return shared_map[text_val]
        new_idx = len(si_list)
        si_el = ET.Element("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si")
        t_el = ET.SubElement(si_el, "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
        t_el.text = text_val
        ss_root.append(si_el)
        si_list.append(si_el)
        shared_map[text_val] = new_idx
        added_count += 1
        return new_idx

    def set_cell_in_row(row_xml, cell_ref, val, is_str=False):
        pat = re.compile(rf'(<c\b[^>]*\br="{cell_ref}"[^>]*?)(?:/>|>(.*?)</c>)', re.DOTALL)
        m = pat.search(row_xml)
        if not m:
            return row_xml
        open_tag = re.sub(r'\s+t="[^"]*"', "", m.group(1))
        if is_str:
            sst_idx = get_or_add_sst(val)
            new_cell = f'{open_tag} t="s"><v>{sst_idx}</v></c>'
        else:
            num_str = str(int(val)) if isinstance(val, (int, float)) and float(val).is_integer() else str(round(float(val), 4))
            new_cell = f"{open_tag}><v>{num_str}</v></c>"
        return row_xml[: m.start()] + new_cell + row_xml[m.end() :]

    row_updates = {
        2: [("I2", f"{block_name} ISI KAYBI HESABI", True)],
    }

    for idx, m in enumerate(mahals):
        r0 = 7 + 14 * idx
        name_col = "H" if idx == 0 else "I"
        row_updates[r0] = [
            (f"G{r0}", m["code"], True),
            (f"{name_col}{r0}", m["name"], True),
        ]

        win_slots = [("DPK", 0.0, 0.0), ("DPG", 0.0, 0.0), ("DPD", 0.0, 0.0)]
        for w_i, (w_code, w_w, w_h) in enumerate(m["wins"][:3]):
            win_slots[w_i] = (w_code, w_w, w_h)

        ik_w, ik_h = m.get("ik", (0.0, 0.0))

        row_updates[r0 + 1] = [(f"B{r0+1}", "DH1", True), (f"C{r0+1}", m["dh1"], False)]
        row_updates[r0 + 2] = [(f"B{r0+2}", "DH3", True), (f"C{r0+2}", m["dh3"], False)]
        row_updates[r0 + 3] = [(f"B{r0+3}", "DH2", True), (f"C{r0+3}", m["dh2"], False)]
        row_updates[r0 + 4] = [(f"B{r0+4}", "DO1", True), (f"C{r0+4}", m.get("do1", 0.0), False)]
        row_updates[r0 + 5] = [(f"B{r0+5}", "İK", True), (f"C{r0+5}", ik_w, False), (f"F{r0+5}", ik_h, False)]
        row_updates[r0 + 6] = [(f"B{r0+6}", m["ceil_code"], True), (f"C{r0+6}", m["ceil_area"], False)]
        row_updates[r0 + 7] = [(f"B{r0+7}", m["floor_code"], True), (f"C{r0+7}", m["floor_area"], False)]
        row_updates[r0 + 8] = [(f"B{r0+8}", win_slots[0][0], True), (f"C{r0+8}", win_slots[0][1], False), (f"F{r0+8}", win_slots[0][2], False)]
        row_updates[r0 + 9] = [(f"B{r0+9}", win_slots[1][0], True), (f"C{r0+9}", win_slots[1][1], False), (f"F{r0+9}", win_slots[1][2], False)]
        row_updates[r0 + 10] = [(f"B{r0+10}", win_slots[2][0], True), (f"C{r0+10}", win_slots[2][1], False), (f"F{r0+10}", win_slots[2][2], False)]
        row_updates[r0 + 11] = [(f"C{r0+11}", m["it1_area"], False), (f"P{r0+11}", float(m["yon"]), False)]
        row_updates[r0 + 12] = [(f"H{r0+12}", 1.0, False)]

    def replace_row(match):
        r_num = int(match.group(1))
        if r_num not in row_updates:
            return match.group(0)
        r_xml = match.group(0)
        for c_ref, val, is_s in row_updates[r_num]:
            r_xml = set_cell_in_row(r_xml, c_ref, val, is_s)
        return r_xml

    s2_new = re.sub(r'<row\b[^>]*\br="(\d+)"[^>]*>.*?</row>', replace_row, s2_xml, flags=re.DOTALL)

    ss_root.attrib["count"] = str(int(ss_root.attrib.get("count", len(si_list))) + added_count + 500)
    ss_root.attrib["uniqueCount"] = str(len(si_list))
    ss_new_bytes = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + ET.tostring(ss_root, encoding="utf-8")

    wb_new = re.sub(r'(<calcPr\b[^/>]*)(/?>)', r'\1 fullCalcOnLoad="1"\2', wb_xml)

    with zipfile.ZipFile(template_path, "r") as zin, zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "xl/worksheets/sheet2.xml":
                zout.writestr(item, s2_new.encode("utf-8"))
            elif item.filename == "xl/sharedStrings.xml":
                zout.writestr(item, ss_new_bytes)
            elif item.filename == "xl/workbook.xml":
                zout.writestr(item, wb_new.encode("utf-8"))
            else:
                zout.writestr(item, zin.read(item.filename))


class OtomasyonHTTPHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/durum", "/health"):
            self._send_json(200, {
                "durum": "aktif",
                "servis": "7/24 DWG-DXF Isı Kaybı Excel (.xlsm) Otomasyon Motoru",
                "sablon_mevcut": os.path.exists(DEFAULT_TEMPLATE),
            })
        else:
            self._send_json(404, {"hata": "Endpoint bulunamadı"})

    def _anahtar_gecerli(self):
        return (not API_ANAHTARI) or self.headers.get("X-API-Key", "") == API_ANAHTARI

    def _yetki_reddet(self, telegram_id, kredi_dus=False):
        if not self._anahtar_gecerli():
            self._send_json(401, {"basarili": False, "mesaj": "Geçersiz API anahtarı."})
            return True
        ok, mesaj = yetki_kontrol(telegram_id, kredi_dus=kredi_dus)
        if not ok:
            self._send_json(403, {"basarili": False, "mesaj": mesaj})
            return True
        return False

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/dosya-yukle":
            chat_id = (qs.get("chat_id", ["genel"])[0] or "genel").strip()
            telegram_id = (qs.get("telegram_id", [chat_id])[0] or chat_id).strip()
            if self._yetki_reddet(telegram_id):
                return
            file_name_qs = (qs.get("file_name", ["dosya"])[0] or "dosya").strip()
            session_dir = os.path.join(SESSIONS_DIR, re.sub(r"[^a-zA-Z0-9_-]", "_", chat_id))
            os.makedirs(session_dir, exist_ok=True)

            ctype = self.headers.get("Content-Type", "")
            content_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_len)

            saved_name = os.path.basename(file_name_qs)
            file_bytes = raw_body

            if "multipart/form-data" in ctype:
                mime = BytesParser(policy=email_default_policy).parsebytes(
                    b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw_body
                )
                for part in mime.iter_parts():
                    if part.get_param("name", header="content-disposition") == "file":
                        fn = part.get_filename()
                        if fn and saved_name == "dosya":
                            saved_name = os.path.basename(fn)
                        file_bytes = part.get_payload(decode=True)
                        break

            dst_file = os.path.join(session_dir, saved_name)
            with open(dst_file, "wb") as f:
                f.write(file_bytes)

            mevcut_dosyalar = os.listdir(session_dir)
            self._send_json(200, {
                "basarili": True,
                "chat_id": chat_id,
                "kaydedilen_dosya": saved_name,
                "oturumdaki_dosyalar": mevcut_dosyalar,
                "mesaj": f"✅ '{saved_name}' başarıyla yüklendi.\n📂 Oturumdaki dosyalar ({len(mevcut_dosyalar)}): {', '.join(mevcut_dosyalar)}\n\n🚀 Tüm dosyaları gönderdiyseniz /hesapla yazarak Excel (.xlsm) üretimini başlatabilirsiniz."
            })
            return

        if parsed.path == "/temizle":
            content_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_len).decode("utf-8") or "{}") if content_len > 0 else {}
            chat_id = str(body.get("chat_id", "genel"))
            if self._yetki_reddet(str(body.get("telegram_id", chat_id))):
                return
            session_dir = os.path.join(SESSIONS_DIR, re.sub(r"[^a-zA-Z0-9_-]", "_", chat_id))
            if os.path.exists(session_dir):
                shutil.rmtree(session_dir, ignore_errors=True)
            os.makedirs(session_dir, exist_ok=True)
            self._send_json(200, {"basarili": True, "mesaj": "Oturum dosyaları temizlendi."})
            return

        if parsed.path == "/hesapla":
            content_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_len).decode("utf-8") or "{}") if content_len > 0 else {}
            chat_id = str(body.get("chat_id", "genel"))
            telegram_id = str(body.get("telegram_id", chat_id))
            if self._yetki_reddet(telegram_id):
                return
            session_dir = os.path.join(SESSIONS_DIR, re.sub(r"[^a-zA-Z0-9_-]", "_", chat_id))
            os.makedirs(session_dir, exist_ok=True)

            kapi_ok, kapi_mesaj = proje_kapisi.kontrol(session_dir, HERKESE_ACIK_TEST_MODU)

            custom_xlsm = glob.glob(os.path.join(session_dir, "*.xlsm"))
            custom_xlsm = [p for p in custom_xlsm if not os.path.basename(p).startswith(("A BLOK", "B BLOK", "C BLOK", "Klima Hesap"))]
            tpl_path = custom_xlsm[0] if custom_xlsm else DEFAULT_TEMPLATE

            out_files = []
            if kapi_ok:
                for block_name, cfg in BLOCK_CONFIGS.items():
                    mahals = build_all_floors_for_block(cfg)
                    out_name = cfg["filename"]
                    out_path = os.path.join(session_dir, out_name)
                    generate_xlsm_pure_python(tpl_path, out_path, block_name, mahals)
                    with open(out_path, "rb") as f:
                        b64_data = base64.b64encode(f.read()).decode("ascii")
                    out_files.append({
                        "blok": block_name,
                        "dosya_adi": out_name,
                        "mahal_sayisi": len(mahals),
                        "toplam_isi_kaybi_w": "Excel açılınca hesaplanır",
                        "base64": b64_data,
                    })
            else:
                dxfler = [os.path.join(session_dir, d) for d in os.listdir(session_dir) if d.lower().endswith(".dxf")]
                if dxfler and any(proje_kapisi._mahal_no_katmani_var_mi(p) for p in dxfler):
                    from dxf_olcu_okuyucu import pilot_veya_dinamik_hesapla
                    sonuc = pilot_veya_dinamik_hesapla(dxfler, body.get("parametreler") or {})
                    mahals = sonuc["mahaller"]
                    if mahals:
                        out_name = "Klima Hesap Tablosu - Isi Kaybi.xlsm"
                        out_path = os.path.join(session_dir, out_name)
                        generate_xlsm_pure_python(tpl_path, out_path, "PROJE", mahals)
                        with open(out_path, "rb") as f:
                            b64_data = base64.b64encode(f.read()).decode("ascii")
                        out_files.append({
                            "blok": "PROJE",
                            "dosya_adi": out_name,
                            "mahal_sayisi": len(mahals),
                            "toplam_isi_kaybi_w": "Excel açılınca hesaplanır",
                            "mahaller": [
                                {"kod": m["code"], "ad": m["name"], "alan": m["area"],
                                 "dh1": m["dh1"], "dh2": m["dh2"], "dh3": m["dh3"], "do1": m["do1"]}
                                for m in mahals
                            ],
                            "base64": b64_data,
                        })
                if not out_files:
                    self._send_json(422, {"basarili": False, "chat_id": chat_id, "mesaj": kapi_mesaj})
                    return

            yetki_kontrol(telegram_id, kredi_dus=True)

            self._send_json(200, {
                "basarili": True,
                "chat_id": chat_id,
                "blok_sayisi": len(out_files),
                "dosyalar": out_files,
            })
            return

        self._send_json(404, {"hata": "Bilinmeyen istek yolu"})


def run_server(host="0.0.0.0", port=8000):
    server = ThreadingHTTPServer((host, port), OtomasyonHTTPHandler)
    print(f"============================================================")
    print(f" 7/24 DWG-DXF -> ISI KAYBI EXCEL (.xlsm) API SUNUCUSU AKTIF ")
    print(f" Adres: http://{host}:{port}")
    print(f" Durum Kontrol: http://localhost:{port}/durum")
    print(f"============================================================")
    server.serve_forever()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    run_server("0.0.0.0", port)
