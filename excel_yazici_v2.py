"""
Excel Yazıcı v2 - Ticari Isı Kaybı Hesap Tablosu (.xlsm) Üretim Motoru.

Özellikler:
- Microsoft Excel gerektirmeden saf Python (ZIP + XML + SharedStrings) ile çalışır.
- Şablondaki tüm makroları (vbaProject.bin), şifreleri, biçimleri ve formülleri korur.
- Döşeme tipine göre kesin kural uygular:
  * Plak / Kirişli döşeme ('plak' / 'diger'): DH1 (Dış Duvar), DH2 (Kolon) ve DH3 (Kiriş) sütunlarının üçünü de doldurur.
  * Asmolen döşeme ('asmolen'): Sadece DH1 ve DH2 sütunlarını doldurur, DH3 (Kiriş) sütununu 0.00 yaparak pasifize eder.
- Hibrit Tavan (CA1, CA2, CC1) ve Karma Bina Taban (TO1, TG1, TT1) kombinasyonlarını destekler:
  * Tek katlı yapılarda hem tavan (CC1/CA1/CA2) hem taban (TO1/TG1/TT1) aynı anda yazılır.
  * Karma yapılarda (Konut+Dükkan) parçalı taban (TO1 + TG1) veya hibrit çatı (CC1 + CA2) aynı mahalde iki ayrı yatay bileşen satırına (r0+6 ve r0+7) işlenir.
- Otomatik Yön Tayin Motorundan gelen DPK, DPB, DPD, DPG pencereleri ile DK (Dış Kapı) ve DP (Menfez/Küçük Pencere) açıklıklarını doğrudan ilgili hücrelere yazar.
"""
import re
import xml.etree.ElementTree as ET
import zipfile

ET.register_namespace("", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


TAVAN_KODLARI = ("CA1", "CA2", "CA3", "CC1", "CC2")
TABAN_KODLARI = ("TO1", "TO2", "TG1", "TG2", "TT1", "TT2", "TG3")


def _yatay_bilesenleri_coz(m):
    """
    Bir mahalin tavan (CA1/CA2/CA3/CC1/CC2) ve taban (TO1/TO2/TG1/TG2/TT1) alanlarını Excel'deki
    iki yatay ısı kaybı satırına (r0+6 ve r0+7) ve ara kat alan satırına (r0+11) yerleştirir.
    """
    surfaces = []
    # 1. Birincil ve ikincil (hibrit) tavan
    c1_code = (m.get("ceil_code") or "").strip().upper()
    c1_area = round(float(m.get("ceil_area", 0.0) or 0.0), 2)
    if c1_code and c1_area > 0:
        surfaces.append((c1_code, c1_area))

    c2_code = (m.get("ceil2_code") or "").strip().upper()
    c2_area = round(float(m.get("ceil2_area", 0.0) or 0.0), 2)
    if c2_code and c2_area > 0:
        surfaces.append((c2_code, c2_area))

    # 2. Birincil ve ikincil (karma bina TO1/TO2 + TG1/TG2) taban
    f1_code = (m.get("floor_code") or "").strip().upper()
    f1_area = round(float(m.get("floor_area", 0.0) or 0.0), 2)
    if f1_code and f1_area > 0:
        surfaces.append((f1_code, f1_area))

    f2_code = (m.get("floor2_code") or "").strip().upper()
    f2_area = round(float(m.get("floor2_area", 0.0) or 0.0), 2)
    if f2_code and f2_area > 0:
        surfaces.append((f2_code, f2_area))

    # Varsayılan satır kodları (alan 0 olsa bile şablonda geçerli HLOOKUP kodu bulunmalı)
    default_s1 = c1_code if c1_code in TAVAN_KODLARI else "CA1"
    default_s2 = f1_code if f1_code in TABAN_KODLARI else "TO1"

    if len(surfaces) == 0:
        slot1 = (default_s1, 0.0)
        slot2 = (default_s2, 0.0)
    elif len(surfaces) == 1:
        kod, alan = surfaces[0]
        if kod in TAVAN_KODLARI:
            slot1 = (kod, alan)
            slot2 = (default_s2, 0.0)
        else:
            slot1 = (default_s1, 0.0)
            slot2 = (kod, alan)
    else:
        # 2 veya daha fazla yüzey (ör. 1 katlı binada CC1 + TO1, veya Karma binada TG1/TG2 + TO1/TO2, veya Hibrit CC1 + CA2/CA3)
        slot1 = surfaces[0]
        slot2 = surfaces[1]

    it1_area = round(float(m.get("it1_area", 0.0) or 0.0), 2)
    toplam_alan = round(float(m.get("area", 0.0) or 0.0), 2)
    if slot1[1] == 0.0 and slot2[1] == 0.0 and it1_area == 0.0:
        it1_area = toplam_alan
    elif it1_area == 0.0 and 0.0 < (slot1[1] + slot2[1]) < (toplam_alan - 0.05):
        it1_area = round(max(0.0, toplam_alan - slot1[1] - slot2[1]), 2)
    return slot1, slot2, it1_area


def generate_xlsm_pure_python(template_path, output_path, block_name, mahals, doseme_tipi=None):
    """
    Şablon .xlsm dosyasından makroları ve formülleri bozmadan yeni ısı kaybı tablosu üretir.
    """
    asmolen_zorla = str(doseme_tipi or "").strip().lower() == "asmolen"

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
        2: [("I2", f"{block_name} ISI KAYBI HESABI", True), ("BD2", "CC2", True)],
        3: [("AT3", 2.5, False), ("BD3", 0.0, False)],
        4: [("BD4", 0.3, False)],
        5: [("BD5", 2.0, False)],
    }

    for idx, m in enumerate(mahals):
        r0 = 7 + 14 * idx
        name_col = "H" if idx == 0 else "I"
        row_updates[r0] = [
            (f"G{r0}", m["code"], True),
            (f"{name_col}{r0}", m["name"], True),
        ]

        # Pencere ve Dış Kapı (DK) / Menfez (DP) yuvaları (3 adet: r0+8, r0+9, r0+10)
        win_slots = [("DPK", 0.0, 0.0), ("DPG", 0.0, 0.0), ("DPD", 0.0, 0.0)]
        for w_i, w_item in enumerate(m.get("wins", [])[:3]):
            w_code, w_w, w_h = w_item[0], float(w_item[1]), float(w_item[2])
            win_slots[w_i] = (str(w_code).strip().upper(), w_w, w_h)

        ik_w, ik_h = m.get("ik", (0.0, 0.0))
        wall_code = str(m.get("wall_code", "DH1") or "DH1").strip().upper()
        if wall_code not in ("DH1", "DT1"):
            wall_code = "DH1"
        dh1_val = float(m.get("dh1", 0.0) or 0.0)
        dh2_val = float(m.get("dh2", 0.0) or 0.0)
        # Asmolen döşemede DH3 kesin olarak 0.00 (pasif) yapılır
        if asmolen_zorla or str(m.get("doseme", "")).lower() == "asmolen":
            dh3_val = 0.0
        else:
            dh3_val = float(m.get("dh3", 0.0) or 0.0)
        do1_val = float(m.get("do1", 0.0) or 0.0)

        slot1, slot2, it1_area = _yatay_bilesenleri_coz(m)

        row_updates[r0 + 1] = [(f"B{r0+1}", wall_code, True), (f"C{r0+1}", dh1_val, False)]
        row_updates[r0 + 2] = [(f"B{r0+2}", "DH3", True), (f"C{r0+2}", dh3_val, False)]
        row_updates[r0 + 3] = [(f"B{r0+3}", "DH2", True), (f"C{r0+3}", dh2_val, False)]
        row_updates[r0 + 4] = [(f"B{r0+4}", "DO1", True), (f"C{r0+4}", do1_val, False)]
        row_updates[r0 + 5] = [(f"B{r0+5}", "İK", True), (f"C{r0+5}", float(ik_w), False), (f"F{r0+5}", float(ik_h), False)]
        row_updates[r0 + 6] = [(f"B{r0+6}", slot1[0], True), (f"C{r0+6}", slot1[1], False)]
        row_updates[r0 + 7] = [(f"B{r0+7}", slot2[0], True), (f"C{r0+7}", slot2[1], False)]
        row_updates[r0 + 8] = [(f"B{r0+8}", win_slots[0][0], True), (f"C{r0+8}", win_slots[0][1], False), (f"F{r0+8}", win_slots[0][2], False)]
        row_updates[r0 + 9] = [(f"B{r0+9}", win_slots[1][0], True), (f"C{r0+9}", win_slots[1][1], False), (f"F{r0+9}", win_slots[1][2], False)]
        row_updates[r0 + 10] = [(f"B{r0+10}", win_slots[2][0], True), (f"C{r0+10}", win_slots[2][1], False), (f"F{r0+10}", win_slots[2][2], False)]
        row_updates[r0 + 11] = [(f"C{r0+11}", it1_area, False), (f"P{r0+11}", float(m.get("yon", 0.0)), False)]
        row_updates[r0 + 12] = [(f"H{r0+12}", 1.0, False)]

    def replace_row(match):
        r_num = int(match.group(1))
        if r_num not in row_updates:
            return match.group(0)
        r_xml = match.group(0)
        for c_ref, val, is_s in row_updates[r_num]:
            r_xml = set_cell_in_row(r_xml, c_ref, val, is_s)
        return r_xml

    s2_xml_new = re.sub(r'<row\b[^>]*\br="(\d+)"[^>]*>.*?</row>', replace_row, s2_xml, flags=re.DOTALL)

    ss_Root_Count = int(ss_root.attrib.get("count", len(si_list))) + added_count
    ss_root.attrib["count"] = str(ss_Root_Count)
    ss_root.attrib["uniqueCount"] = str(len(si_list))
    ss_xml_new = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(ss_root, encoding="unicode")

    if "fullCalcOnLoad" in wb_xml:
        wb_xml_new = re.sub(r'fullCalcOnLoad="[^"]*"', 'fullCalcOnLoad="1"', wb_xml)
    else:
        wb_xml_new = wb_xml.replace("<calcPr ", '<calcPr fullCalcOnLoad="1" ')

    with zipfile.ZipFile(template_path, "r") as zin, zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/sharedStrings.xml":
                data = ss_xml_new.encode("utf-8")
            elif item.filename == "xl/worksheets/sheet2.xml":
                data = s2_xml_new.encode("utf-8")
            elif item.filename == "xl/workbook.xml":
                data = wb_xml_new.encode("utf-8")
            zout.writestr(item, data)
