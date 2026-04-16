from playwright.sync_api import sync_playwright
import csv
import os

base_dir = r"T:\KFWIS Staff\Jones\CODEING\PVA"
input_path = os.path.join(base_dir, "ParcelID.csv")
output_path = os.path.join(base_dir, "owner_output.csv")

COUNTY_APP_IDS = {
    "Adair": "903", "Allen": "952", "Anderson": "784", "Ballard": "805",
    "Barren": "785", "Bath": "934", "Bell": "968", "Bourbon": "949",
    "Boyd": "786", "Boyle": "876", "Bracken": "970", "Breckinridge": "787",
    "Bullitt": "943", "Butler": "967", "Caldwell": "957", "Carlisle": "969",
    "Carter": "894", "Casey": "891", "Clark": "869", "Clay": "1158",
    "Clinton": "1074", "Crittenden": "941", "Edmonson": "965", "Elliott": "978",
    "Estill": "1162", "Fleming": "1137", "Floyd": "1000", "Franklin": "1025",
    "Fulton": "966", "Gallatin": "901", "Garrard": "1145", "Grant": "893",
    "Green": "956", "Greenup": "882", "Hancock": "878", "Hardin": "879",
    "Harlan": "877", "Harrison": "1141", "Hart": "926", "Henderson": "884",
    "Henry": "1070", "Hopkins": "880", "Jackson": "1106", "Jessamine": "864",
    "Johnson": "883", "Knott": "1184", "Knox": "971", "Larue": "1084",
    "Laurel": "621", "Leslie": "973", "Letcher": "972", "Lewis": "954",
    "Lincoln": "944", "Logan": "905", "Lyon": "955", "Madison": "889",
    "Mason": "904", "McCracken": "854", "McCreary": "999", "McLean": "888",
    "Meade": "874", "Mercer": "1103", "Metcalfe": "935", "Monroe": "962",
    "Montgomery": "886", "Morgan": "806", "Muhlenberg": "875", "Nelson": "871",
    "Oldham": "895", "Owen": "892", "Pendleton": "1157", "Perry": "870",
    "Pike": "951", "Pulaski": "961", "Rockcastle": "1226", "Rowan": "938",
    "Scott": "948", "Shelby": "890", "Taylor": "950", "Trigg": "945",
    "Union": "881", "Warren": "1054", "Washington": "610", "Wayne": "906",
    "Woodford": "924",
}

def load_records_from_csv(csv_path):
    records = []

    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        fieldnames = [name.strip().lower() for name in (reader.fieldnames or [])]

        if "county" not in fieldnames or "parcel_id" not in fieldnames:
            raise ValueError("ParcelID.csv must contain headers: county, parcel_id")

        for row in reader:
            normalized_row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items()}

            county = normalized_row.get("county", "").title()
            parcel_id = normalized_row.get("parcel_id", "")

            if county and parcel_id:
                records.append({
                    "county": county,
                    "parcel_id": parcel_id,
                })

    return records

def build_url(county, parcel_id):
    app_id = COUNTY_APP_IDS.get(county)
    if not app_id:
        raise ValueError(f"County not found: {county}")
    return f"https://beacon.schneidercorp.com/Application.aspx?AppID={app_id}&PageTypeID=4&KeyValue={parcel_id}"

def click_agree_if_present(page):
    possible_selectors = [
        "text=Agree",
        "text=I Agree",
        "text=Accept",
        "button:has-text('Agree')",
        "button:has-text('I Agree')",
        "input[value='I Agree']",
        "input[value='Agree']",
    ]

    for selector in possible_selectors:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=1200):
                loc.click()
                page.wait_for_load_state("domcontentloaded", timeout=8000)
                return True
        except:
            pass
    return False

def get_owner_block(page):
    body_text = page.locator("body").inner_text()
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]

    for i, line in enumerate(lines):
        if line.lower() == "owner":
            idx = i + 1

            if idx < len(lines) and lines[idx].lower() == "primary owner":
                idx += 1

            owner_name = lines[idx] if idx < len(lines) else ""
            owner_addr1 = lines[idx + 1] if idx + 1 < len(lines) else ""
            city_state_zip = lines[idx + 2] if idx + 2 < len(lines) else ""

            return {
                "owner_name": owner_name,
                "owner_addr1": owner_addr1,
                "city_state_zip": city_state_zip,
            }

    return {
        "owner_name": "",
        "owner_addr1": "",
        "city_state_zip": "",
    }

def load_processed_records(csv_path):
    processed = set()
    if not os.path.exists(csv_path):
        return processed

    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            county = (row.get("county") or "").strip()
            parcel_id = (row.get("parcel_id") or "").strip()
            if county and parcel_id:
                processed.add((county, parcel_id))
    return processed

records = load_records_from_csv(input_path)
processed_records = load_processed_records(output_path)

records_to_run = [
    r for r in records
    if (r["county"], r["parcel_id"]) not in processed_records
]

file_exists = os.path.exists(output_path)
write_header = not file_exists or os.path.getsize(output_path) == 0

print(f"Loaded from ParcelID.csv: {len(records)}")
print(f"Already processed: {len(processed_records)}")
print(f"Remaining this run: {len(records_to_run)}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)

    with open(output_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if write_header:
            writer.writerow([
                "county",
                "app_id",
                "parcel_id",
                "owner_name",
                "owner_addr1",
                "city_state_zip",
                "url",
                "status",
            ])

        bad_returns_in_a_row = 0
        max_bad_returns_in_a_row = 10

        for n, record in enumerate(records_to_run, start=1):
            county = record["county"]
            parcel_id = record["parcel_id"]
            app_id = COUNTY_APP_IDS.get(county, "")
            url = build_url(county, parcel_id)

            page = browser.new_page()

            try:
                print(f"[{n}/{len(records_to_run)}] Processing {county} - {parcel_id}")
                page.goto(url, timeout=45000, wait_until="domcontentloaded")

                click_agree_if_present(page)

                owner_info = get_owner_block(page)
                status = "OK" if owner_info["owner_name"] else "OWNER NOT FOUND"

                if status == "OK":
                    bad_returns_in_a_row = 0
                else:
                    bad_returns_in_a_row += 1
                    print("Owner not found. First 1000 chars of page text:")
                    print(page.locator("body").inner_text()[:1000])

                writer.writerow([
                    county,
                    app_id,
                    parcel_id,
                    owner_info["owner_name"],
                    owner_info["owner_addr1"],
                    owner_info["city_state_zip"],
                    url,
                    status,
                ])
                f.flush()

                if bad_returns_in_a_row >= max_bad_returns_in_a_row:
                    print(f"Stopping early: {bad_returns_in_a_row} bad returns in a row.")
                    break

            except Exception as e:
                print(f"Error on {county} - {parcel_id}: {e}")
                writer.writerow([
                    county,
                    app_id,
                    parcel_id,
                    "",
                    "",
                    "",
                    url,
                    f"ERROR: {e}",
                ])
                f.flush()
                bad_returns_in_a_row += 1

                if bad_returns_in_a_row >= max_bad_returns_in_a_row:
                    print(f"Stopping early: {bad_returns_in_a_row} bad returns in a row.")
                    break

            finally:
                page.close()

    browser.close()

print(f"\nSaved to: {output_path}")
os.startfile(base_dir)