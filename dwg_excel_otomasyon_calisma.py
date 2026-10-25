import glob
import os
import shutil

# Find the working folder and template backup xlsm
calisma_dir = os.path.dirname(os.path.abspath(__file__))
backup_xlsm = os.path.abspath(os.path.join(calisma_dir, "Klima Hesap Tablosu_HAM_YEDEK.xlsm"))

# Vaziyet Planı (Ö:1/100) Pusula ve Blok Yönelimleri:
# Pusula: Sağ (+X) = KUZEY (DPK, +5%), Sol (-X) = GÜNEY (DPG, -5%), Üst (+Y) = BATI (DPB, 0%), Alt (-Y) = DOĞU (DPD, 0%)
# - A BLOK: Giriş cephesi (01..13) -> KUZEY (DPK, +5%), Arka cephe (01A..13A) -> GÜNEY (DPG, -5%), Yan balkon kapısı (04/04A) -> BATI (DPB)
# - B BLOK: Giriş cephesi (01..13) -> DOĞU (DPD, 0%),   Arka cephe (01A..13A) -> BATI (DPB, 0%),   Yan balkon kapısı (04/04A) -> GÜNEY (DPG)
# - C BLOK: Giriş cephesi (01..13) -> GÜNEY (DPG, -5%), Arka cephe (01A..13A) -> KUZEY (DPK, +5%), Yan balkon kapısı (04/04A) -> BATI (DPB)

YON_MAP = {
    "DPK": 5,
    "DPG": -5,
    "DPD": 0,
    "DPB": 0,
}

BLOCK_CONFIGS = {
    "A BLOK": {
        "front_win": "DPK",  # 01..13 ana cephe (Kuzey, +5%)
        "back_win":  "DPG",  # 01A..13A arka cephe (Güney, -5%)
        "side_win":  "DPB",  # 04 ve 04A yan balkon kapısı (Batı)
        "filename":  "A BLOK - Klima Hesap Tablosu.xlsm",
    },
    "B BLOK": {
        "front_win": "DPD",  # 01..13 ana cephe (Doğu, 0%)
        "back_win":  "DPB",  # 01A..13A arka cephe (Batı, 0%)
        "side_win":  "DPG",  # 04 ve 04A yan balkon kapısı (Güney)
        "filename":  "B BLOK - Klima Hesap Tablosu.xlsm",
    },
    "C BLOK": {
        "front_win": "DPG",  # 01..13 ana cephe (Güney, -5%)
        "back_win":  "DPK",  # 01A..13A arka cephe (Kuzey, +5%)
        "side_win":  "DPB",  # 04 ve 04A yan balkon kapısı (Batı)
        "filename":  "C BLOK - Klima Hesap Tablosu.xlsm",
    },
}


def base_13_rooms(is_normal_kat=False, is_lower_a=False, block_cfg=None):
    """
    Returns the 13 rooms of one half-floor (01..13 or 01A..13A) for a given block orientation.
    - Antre+Hol mahallerine bu projede kod verilmediği için dahil edilmez; ileriki projelerde
      kod verildiğinde DO1 + İK (daire giriş kapısı) ile ayrı mahal olarak girilir.
    - 101..501 ve 107..507 (Normal kat ön salonlar): Ön cephe (3.45 m) + Gezilemeyen Teras
      (Saçak İzdüşümü) yan dış duvar devamı (2.70 m) = 6.15 m DH1.
    - DO1 (Isıtılmayan iç ortama bitişik duvarlar - Merdiven/Kat Holü, Yangın Merdiveni, Asansör):
      * 101..501 ve 107..507: 2.90 m (Merdiven holü yan duvar) + 3.70 m (Kat holü koridor duvarı) = 6.60 m DO1
      * 101A..501A ve 107A..507A: 5.60 m (Yangın merdiveni + Asansör yan duvarı) + 3.70 m (Kat holü duvarı) = 9.30 m DO1
      * Z01 ve Z01A: 5.80 m (Bina giriş holü / Yangın merd. yan duvarı) + 3.70 m (Kat holü duvarı) = 9.50 m DO1
      * Z07 ve Z07A: 6.15 m (Bina giriş holü / Yangın merd. yan duvarı) + 3.70 m (Kat holü duvarı) = 9.85 m DO1
    """
    main_win = block_cfg["back_win"] if is_lower_a else block_cfg["front_win"]
    side_win = block_cfg["side_win"]
    main_yon = YON_MAP[main_win]

    salon_left_area = 20.80 if is_normal_kat else 21.50   # Salon net alanı (Antre+Hol kodsuz)
    salon_right_area = 21.00 if is_normal_kat else 23.00  # Salon net alanı (Antre+Hol kodsuz)
    oda11_area = 11.60 if is_normal_kat else 12.00        # Oda 11 (Normal katta 11.60, Zeminde 12.00)

    # 101..501 ve 107..507'de Gezilemeyen Teras (Saçak İzdüşümü) boyunca DH1 2.70 m devam eder:
    if is_normal_kat and not is_lower_a:
        salon_left_dh1 = 3.45 + 2.70   # 6.15 m (Ön cephe 3.45 + Gezilemeyen teras 2.70)
        salon_right_dh1 = 3.45 + 2.70  # 6.15 m (Ön cephe 3.45 + Gezilemeyen teras 2.70)
        salon_left_do1 = 2.90 + 3.70   # 6.60 m (Merdiven/Kat holü ısıtılmayan iç duvar)
        salon_right_do1 = 2.90 + 3.70  # 6.60 m (Merdiven/Kat holü ısıtılmayan iç duvar)
    elif is_normal_kat and is_lower_a:
        salon_left_dh1 = 3.45
        salon_right_dh1 = 3.45
        salon_left_do1 = 5.60 + 3.70   # 9.30 m (Yangın merdiveni + Asansör + Kat holü)
        salon_right_do1 = 5.60 + 3.70  # 9.30 m (Yangın merdiveni + Kat holü)
    else:
        # Zemin Kat (Z01, Z07, Z01A, Z07A)
        salon_left_dh1 = 3.45
        salon_right_dh1 = 3.45
        salon_left_do1 = 5.80 + 3.70   # 9.50 m (Bina giriş holü / Yangın merd. + Kat holü)
        salon_right_do1 = 6.15 + 3.70  # 9.85 m (Bina giriş holü / Yangın merd. + Kat holü)

    rooms = [
        # 01: SALON
        {"num": 1, "name": "SALON", "temp": 20, "area": salon_left_area,
         "dh1": salon_left_dh1, "dh3": salon_left_dh1, "dh2": 0.60, "do1": salon_left_do1, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 2.50, 2.20)]},
        # 02: MUTFAK
        {"num": 2, "name": "MUTFAK", "temp": 20, "area": 8.10,
         "dh1": 2.40, "dh3": 2.40, "dh2": 0.00, "do1": 0.0, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 1.50, 2.20)]},
        # 03: ODA
        {"num": 3, "name": "ODA", "temp": 20, "area": 9.00,
         "dh1": 2.50, "dh3": 2.50, "dh2": 1.00, "do1": 0.0, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 1.50, 2.20)]},
        # 04: ODA (köşe oda, 150/220 pencere + 90/220 balkon kapısı)
        {"num": 4, "name": "ODA", "temp": 20, "area": 13.10,
         "dh1": 8.10, "dh3": 8.10, "dh2": 0.90, "do1": 0.0, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 1.50, 2.20), (side_win, 0.90, 2.20)]},
        # 05: BANYO (Ebeveyn Duş, 24°C)
        {"num": 5, "name": "BANYO", "temp": 24, "area": 2.65,
         "dh1": 1.70, "dh3": 1.70, "dh2": 0.00, "do1": 0.0, "ik": (0.0, 0.0), "yon": 0,
         "wins": []},
        # 06: BANYO (Genel Banyo, 24°C)
        {"num": 6, "name": "BANYO", "temp": 24, "area": 4.00,
         "dh1": 0.00, "dh3": 0.00, "dh2": 0.00, "do1": 0.0, "ik": (0.0, 0.0), "yon": 0,
         "wins": []},
        # 07: SALON
        {"num": 7, "name": "SALON", "temp": 20, "area": salon_right_area,
         "dh1": salon_right_dh1, "dh3": salon_right_dh1, "dh2": 0.60, "do1": salon_right_do1, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 2.50, 2.20)]},
        # 08: MUTFAK
        {"num": 8, "name": "MUTFAK", "temp": 20, "area": 9.50,
         "dh1": 2.55, "dh3": 2.55, "dh2": 0.00, "do1": 0.0, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 1.50, 2.20)]},
        # 09: ODA
        {"num": 9, "name": "ODA", "temp": 20, "area": 9.40,
         "dh1": 2.35, "dh3": 2.35, "dh2": 0.25, "do1": 0.0, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 1.50, 2.20)]},
        # 10: ODA (köşe oda)
        {"num": 10, "name": "ODA", "temp": 20, "area": 9.30,
         "dh1": 5.35, "dh3": 5.35, "dh2": 1.25, "do1": 0.0, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 1.50, 2.20)]},
        # 11: ODA (sağ yan kanat oda)
        {"num": 11, "name": "ODA", "temp": 20, "area": oda11_area,
         "dh1": 5.20, "dh3": 5.20, "dh2": 1.05, "do1": 0.0, "ik": (0.0, 0.0), "yon": main_yon,
         "wins": [(main_win, 1.50, 2.20)]},
        # 12: BANYO (Ebeveyn Duş, 24°C)
        {"num": 12, "name": "BANYO", "temp": 24, "area": 2.70,
         "dh1": 0.00, "dh3": 0.00, "dh2": 0.00, "do1": 0.0, "ik": (0.0, 0.0), "yon": 0,
         "wins": []},
        # 13: BANYO (Genel Banyo, 24°C)
        {"num": 13, "name": "BANYO", "temp": 24, "area": 4.00,
         "dh1": 0.00, "dh3": 0.00, "dh2": 0.00, "do1": 0.0, "ik": (0.0, 0.0), "yon": 0,
         "wins": []},
    ]
    return rooms


def build_all_floors_for_block(block_cfg):
    """
    Builds all 6 floors (156 mahals total) for the given block:
    - Zemin Kat: Z01..Z13, Z01A..Z13A (TO1 taban kaybı var, CC1 = 0)
    - 1. Kat:    101..113, 101A..113A (Ara kat: TO1 = 0, CC1 = 0, İT1 alan)
    - 2. Kat:    201..213, 201A..213A (Ara kat: TO1 = 0, CC1 = 0, İT1 alan)
    - 3. Kat:    301..313, 301A..313A (Ara kat: TO1 = 0, CC1 = 0, İT1 alan)
    - 4. Kat:    401..413, 401A..413A (Ara kat: TO1 = 0, CC1 = 0, İT1 alan)
    - 5. Kat:    501..513, 501A..513A (Çatı katı: CC1 çatılı tavan kaybı var, TO1 = 0)
    """
    mahals = []

    # 1. Zemin Kat (Z01..Z13, Z01A..Z13A)
    for is_a in (False, True):
        suffix = "A" if is_a else ""
        for r in base_13_rooms(is_normal_kat=False, is_lower_a=is_a, block_cfg=block_cfg):
            rc = dict(r)
            rc["code"] = f"Z{r['num']:02d}{suffix}"
            rc["ceil_code"] = "CA1"
            rc["ceil_area"] = 0.0
            rc["floor_code"] = "TO1"
            rc["floor_area"] = r["area"]
            rc["it1_area"] = 0.0
            mahals.append(rc)

    # 2. Ara Katlar: 1. Kat (101..), 2. Kat (201..), 3. Kat (301..), 4. Kat (401..)
    for floor_no in (1, 2, 3, 4):
        for is_a in (False, True):
            suffix = "A" if is_a else ""
            for r in base_13_rooms(is_normal_kat=True, is_lower_a=is_a, block_cfg=block_cfg):
                rc = dict(r)
                rc["code"] = f"{floor_no}{r['num']:02d}{suffix}"
                rc["ceil_code"] = "CC1"
                rc["ceil_area"] = 0.0
                rc["floor_code"] = "TO1"
                rc["floor_area"] = 0.0
                rc["it1_area"] = r["area"]
                mahals.append(rc)

    # 3. 5. Kat (Çatılı Tavan CC1): 501..513, 501A..513A
    for is_a in (False, True):
        suffix = "A" if is_a else ""
        for r in base_13_rooms(is_normal_kat=True, is_lower_a=is_a, block_cfg=block_cfg):
            rc = dict(r)
            rc["code"] = f"5{r['num']:02d}{suffix}"
            rc["ceil_code"] = "CC1"
            rc["ceil_area"] = r["area"]
            rc["floor_code"] = "TO1"
            rc["floor_area"] = 0.0
            rc["it1_area"] = 0.0
            mahals.append(rc)

    return mahals


def populate_with_xlwings(app, src_path, dst_path, block_name, mahals):
    import xlwings as xw
    # Ensure dst_path is not locked by any existing Excel instance before copying
    dst_name = os.path.basename(dst_path).lower()
    for ex_app in xw.apps:
        for b in list(ex_app.books):
            if b.name.lower() == dst_name:
                b.close()

    # Copy untouched template backup to dst_path first, then open in our hidden app instance
    shutil.copy2(src_path, dst_path)
    wb = app.books.open(dst_path)
    sheet = wb.sheets['ISI KAYBI']
    sheet.activate()

    # Turn off screen updating and set calculation to manual during bulk cell writes
    wb.app.screen_updating = False
    wb.app.calculation = "manual"

    # Write Block Name to TESİSİN ADI field at top of ISI KAYBI (I2)
    sheet.range("I2").value = f"{block_name} ISI KAYBI HESABI"

    for idx, m in enumerate(mahals):
        r0 = 7 + 14 * idx

        # 1. Header input cells (G{r0} = Mahal No, H7 or I{r0} = Mahal Adı)
        sheet.range(f"G{r0}").value = m["code"]
        if idx == 0:
            sheet.range(f"H{r0}").value = m["name"]
        else:
            sheet.range(f"I{r0}").value = m["name"]

        # Prepare 3 window slots (rows r0+8, r0+9, r0+10)
        win_slots = [("DPK", 0.0, 0.0), ("DPG", 0.0, 0.0), ("DPD", 0.0, 0.0)]
        for w_i, (w_code, w_w, w_h) in enumerate(m["wins"][:3]):
            win_slots[w_i] = (w_code, w_w, w_h)

        # 2. Component Symbol column B (rows r0+1 .. r0+10)
        b_vals = [
            ["DH1"],
            ["DH3"],
            ["DH2"],
            ["DO1"],
            ["İK"],
            [m["ceil_code"]],
            [m["floor_code"]],
            [win_slots[0][0]],
            [win_slots[1][0]],
            [win_slots[2][0]],
        ]
        sheet.range(f"B{r0+1}:B{r0+10}").value = b_vals

        # 3. Length / Area column C (rows r0+1 .. r0+11, including DO1 at r0+4, İK at r0+5, İT1 at r0+11)
        ik_w, ik_h = m.get("ik", (0.0, 0.0))
        c_vals = [
            [m["dh1"]],
            [m["dh3"]],
            [m["dh2"]],
            [m.get("do1", 0.0)],
            [ik_w],
            [m["ceil_area"]],
            [m["floor_area"]],
            [win_slots[0][1]],
            [win_slots[1][1]],
            [win_slots[2][1]],
            [m["it1_area"]],
        ]
        sheet.range(f"C{r0+1}:C{r0+11}").value = c_vals

        # 4. İK (Internal Door) Height at F{r0+5} and Window Heights at F{r0+8}:F{r0+10}
        sheet.range(f"F{r0+5}").value = ik_h
        f_win_vals = [
            [win_slots[0][2]],
            [win_slots[1][2]],
            [win_slots[2][2]],
        ]
        sheet.range(f"F{r0+8}:F{r0+10}").value = f_win_vals

        # 5. % YÖN input cell (P{r0+11}) and H.Kat input cell (H{r0+12})
        sheet.range(f"P{r0+11}").value = float(m["yon"])
        sheet.range(f"H{r0+12}").value = 1.0

    # Restore automatic calculation and force full calculation in Excel
    wb.app.calculation = "automatic"
    wb.app.calculate()
    wb.app.screen_updating = True

    # Save and close
    wb.save()
    wb.close()


def verify_with_xlwings(app, file_path, expected_count):
    """
    Re-opens the saved .xlsm file in real Excel via xlwings and verifies all mahals in a single bulk read.
    """
    wb = app.books.open(file_path)
    s_isi = wb.sheets["ISI KAYBI"]
    s_cal = wb.sheets["ÇALIŞMA"]

    title_val = s_isi.range("I2").value
    last_row = 7 + 14 * expected_count - 1
    grid = s_isi.range(f"G7:S{last_row}").value

    errors = []
    results = []
    total_q = 0.0

    for idx in range(expected_count):
        base = 14 * idx
        r0 = 7 + base
        code = grid[base][0]
        name = grid[base][1] if idx == 0 else grid[base][2]
        temp = grid[base][5]
        q_iletim = grid[base + 11][11]
        q_inf = grid[base + 12][11]
        q_tot = grid[base + 13][11]
        area = grid[base + 13][12]

        if code is None or name is None or temp not in (20, 24) or q_tot is None or q_tot < 0 or area is None or area <= 0:
            errors.append(f"Mahal idx={idx} (row {r0}): code={code}, name={name}, temp={temp}, q_tot={q_tot}, area={area}")
        total_q += float(q_tot or 0.0)
        results.append((code, name, temp, area, q_iletim, q_inf, q_tot))

    cal_first = s_cal.range("C5:G6").value
    cal_last = s_cal.range(f"C{5 + expected_count - 1}:G{5 + expected_count - 1}").value
    wb.close()

    if errors:
        raise RuntimeError(f"Verification failed for {file_path} with {len(errors)} errors:\n" + "\n".join(errors[:10]))

    print(f"[VERIFY OK] '{os.path.basename(file_path)}' | Başlık='{title_val}' | Mahal Sayısı={expected_count} | Toplam Isı Kaybı={total_q:,.1f} W")
    print(f"            İlk Mahal ('ÇALIŞMA' C5:G5): {cal_first[0]} | Son Mahal (C{4+expected_count}:G{4+expected_count}): {cal_last}")
    return results, total_q


def main():
    import xlwings as xw
    app = xw.App(visible=False, add_book=False)
    app.display_alerts = False
    try:
        for block_name, cfg in BLOCK_CONFIGS.items():
            mahals = build_all_floors_for_block(cfg)
            dst_path = os.path.abspath(os.path.join(calisma_dir, cfg["filename"]))
            print(f"\n--- {block_name} ({len(mahals)} mahal: Zemin + 1..5. Kat) hazırlanıyor: {dst_path} ---")
            populate_with_xlwings(app, backup_xlsm, dst_path, block_name, mahals)
            verify_with_xlwings(app, dst_path, len(mahals))
    finally:
        app.quit()


if __name__ == "__main__":
    main()
