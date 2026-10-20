import os
import sys
import time
import math
import argparse
import sqlite3
import logging
import requests
import ijson
from datetime import datetime, timedelta

# --- CLI & Runtime Options ---
parser = argparse.ArgumentParser(description="WoW: Forever Market Sentinel Daemon")
parser.add_argument("--region", default=os.environ.get("BLIZZARD_REGION", "us"), help="Target region (us, eu, kr, tw)")
parser.add_argument("--cluster", default=os.environ.get("BLIZZARD_CLUSTER", "mega"), help="Target ruleset/cluster (mega, normal, pvp, hardcore)")
parser.add_argument("--commodities", action="store_true", help="Force regional commodities pool endpoint")
parser.add_argument("--refresh-cluster", action="store_true", help="Force rediscovery of connected realm / mega-cluster ID")
args, _ = parser.parse_known_args()

# --- Configuration ---
CLIENT_ID = os.environ.get("BLIZZARD_CLIENT_ID")
CLIENT_SECRET = os.environ.get("BLIZZARD_CLIENT_SECRET")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

REGION = args.region.lower()
NAMESPACE = f"dynamic-{REGION}"
POLL_INTERVAL_SECONDS = 900  # 15 minutes
RETENTION_DAYS = 30

ACTIVE_DURATIONS = {"MEDIUM", "LONG"}
PRICE_DISCOUNT_PERCENT = 15.0
VOLUME_Z_SCORE_THRESHOLD = 2.0

DEFAULT_WATCHLIST = [
    (13468, "Black Lotus", 4),
    (13463, "Dreamfoil", 100),
    (14344, "Large Brilliant Shard", 15)
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

ACTIVE_ALERTS = {}
OAUTH_CACHE = {
    "access_token": None,
    "expires_at": 0
}


def format_copper(copper_amount: int) -> str:
    gold = copper_amount // 10000
    silver = (copper_amount % 10000) // 100
    copper = copper_amount % 100
    parts = []
    if gold > 0:
        parts.append(f"{gold}g")
    if silver > 0 or gold > 0:
        parts.append(f"{silver}s")
    parts.append(f"{copper}c")
    return " ".join(parts)


def init_db():
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            item_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            min_surge_units INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)

    cursor.execute("SELECT COUNT(*) FROM watchlist")
    if cursor.fetchone()[0] == 0:
        now_iso = datetime.utcnow().isoformat()
        cursor.executemany("""
            INSERT INTO watchlist (item_id, name, min_surge_units, is_active, created_at)
            VALUES (?, ?, ?, 1, ?)
        """, [(item_id, name, min_surge, now_iso) for item_id, name, min_surge in DEFAULT_WATCHLIST])
        logging.info("Seeded default items into watchlist table.")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            timestamp TEXT,
            item_id INTEGER,
            item_name TEXT,
            min_unit_price INTEGER,
            total_quantity INTEGER,
            qty_short INTEGER DEFAULT 0,
            qty_medium INTEGER DEFAULT 0,
            qty_long INTEGER DEFAULT 0,
            qty_very_long INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history_daily (
            date TEXT,
            item_id INTEGER,
            item_name TEXT,
            min_unit_price INTEGER,
            avg_unit_price INTEGER,
            max_unit_price INTEGER,
            avg_quantity INTEGER,
            PRIMARY KEY (date, item_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS http_cache (
            endpoint_key TEXT PRIMARY KEY,
            last_modified TEXT,
            etag TEXT
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_item_time ON price_history(item_id, timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_time ON price_history(timestamp)")
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_history_dow_hour 
        ON price_history(item_id, strftime('%w', timestamp), strftime('%H', timestamp))
    """)

    conn.commit()
    conn.close()


def load_meta_val(key: str) -> str:
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM market_meta WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def save_meta_val(key: str, value: str):
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()
    now_iso = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO market_meta (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
    """, (key, str(value), now_iso))
    conn.commit()
    conn.close()


def load_active_watchlist() -> dict:
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT item_id, name, min_surge_units FROM watchlist WHERE is_active = 1")
    rows = cursor.fetchall()
    conn.close()
    return {row[0]: {"name": row[1], "min_surge_units": row[2]} for row in rows}


def get_oauth_token(force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh and OAUTH_CACHE["access_token"] and now < (OAUTH_CACHE["expires_at"] - 300):
        return OAUTH_CACHE["access_token"]

    logging.info("Requesting fresh Battle.net OAuth access token...")
    url = f"https://{REGION}.battle.net/oauth/token"
    res = requests.post(
        url,
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=10
    )
    res.raise_for_status()
    payload = res.json()

    access_token = payload["access_token"]
    expires_in = payload.get("expires_in", 86400)

    OAUTH_CACHE["access_token"] = access_token
    OAUTH_CACHE["expires_at"] = now + expires_in
    logging.info(f"OAuth token cached for {expires_in // 3600} hours.")
    return access_token


def resolve_market_target(force_refresh: bool = False) -> tuple:
    if args.commodities:
        target_url = f"https://{REGION}.api.blizzard.com/data/wow/auctions/commodities"
        logging.info(f"Configured Regional Commodities Pool: {target_url}")
        return target_url, f"{REGION.upper()}-Commodities"

    cache_key = f"target_url:{REGION}:{args.cluster}"
    if not force_refresh:
        cached_url = load_meta_val(cache_key)
        cached_desc = load_meta_val(f"target_desc:{REGION}:{args.cluster}")
        if cached_url and cached_desc:
            logging.info(f"Loaded cached market cluster target: {cached_desc} ({cached_url})")
            return cached_url, cached_desc

    logging.info(f"Auto-discovering active mega-clusters in region '{REGION}' for ruleset '{args.cluster}'...")
    token = get_oauth_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {"namespace": NAMESPACE, "locale": "en_US"}

    index_url = f"https://{REGION}.api.blizzard.com/data/wow/connected-realm/index"
    res = requests.get(index_url, headers=headers, params=params, timeout=15)
    res.raise_for_status()
    connected_realms = res.json().get("connected_realms", [])

    if not connected_realms:
        raise RuntimeError(f"No connected realms discovered in region '{REGION}'.")

    target_realm_id = None
    target_realm_name = f"{REGION.upper()}-MegaCluster"

    if len(connected_realms) == 1:
        href = connected_realms[0]["href"]
        target_realm_id = href.split("/")[-1].split("?")[0]
        logging.info(f"Single unified mega-realm detected for region: Cluster ID {target_realm_id}")
    else:
        for cr_entry in connected_realms:
            cr_url = cr_entry["href"]
            cr_res = requests.get(cr_url, headers=headers, timeout=10)
            if cr_res.status_code != 200:
                continue

            cr_data = cr_res.json()
            realms = cr_data.get("realms", [])
            realm_names = " / ".join([r.get("name", "") for r in realms])

            if args.cluster in realm_names.lower() or args.cluster == "mega":
                target_realm_id = str(cr_data.get("id"))
                target_realm_name = realm_names or f"Cluster-{target_realm_id}"
                break

        if not target_realm_id:
            first_href = connected_realms[0]["href"]
            target_realm_id = first_href.split("/")[-1].split("?")[0]
            target_realm_name = f"Default-Cluster-{target_realm_id}"

    resolved_url = f"https://{REGION}.api.blizzard.com/data/wow/connected-realm/{target_realm_id}/auctions"
    save_meta_val(cache_key, resolved_url)
    save_meta_val(f"target_desc:{REGION}:{args.cluster}", target_realm_name)

    logging.info(f"Auto-discovery successful. Resolved target to: {target_realm_name} ({resolved_url})")
    return resolved_url, target_realm_name


def prune_and_downsample():
    cutoff = (datetime.utcnow() - timedelta(days=RETENTION_DAYS)).isoformat()
    logging.info(f"Running database retention sweep (Purging raw snapshots older than {cutoff})...")

    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO price_history_daily (date, item_id, item_name, min_unit_price, avg_unit_price, max_unit_price, avg_quantity)
        SELECT 
            substr(timestamp, 1, 10) AS date_str,
            item_id,
            item_name,
            MIN(min_unit_price) AS min_p,
            CAST(AVG(min_unit_price) AS INTEGER) AS avg_p,
            MAX(min_unit_price) AS max_p,
            CAST(AVG(total_quantity) AS INTEGER) AS avg_q
        FROM price_history
        WHERE timestamp < ?
        GROUP BY date_str, item_id
        ON CONFLICT(date, item_id) DO UPDATE SET
            min_unit_price = min(excluded.min_unit_price, price_history_daily.min_unit_price),
            avg_unit_price = excluded.avg_unit_price,
            max_unit_price = max(excluded.max_unit_price, price_history_daily.max_unit_price),
            avg_quantity = excluded.avg_quantity
    """, (cutoff,))

    cursor.execute("DELETE FROM price_history WHERE timestamp < ?", (cutoff,))
    deleted_count = cursor.rowcount

    conn.commit()
    conn.close()

    if deleted_count > 0:
        logging.info(f"Pruned {deleted_count} expired raw snapshots. Running VACUUM...")
        conn = sqlite3.connect("market_data.db")
        conn.execute("VACUUM")
        conn.close()


def load_cached_headers(endpoint_key: str):
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT last_modified, etag FROM http_cache WHERE endpoint_key = ?", (endpoint_key,))
    row = cursor.fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, None)


def save_cached_headers(endpoint_key: str, last_modified: str, etag: str):
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO http_cache (endpoint_key, last_modified, etag)
        VALUES (?, ?, ?)
        ON CONFLICT(endpoint_key) DO UPDATE SET
            last_modified = excluded.last_modified,
            etag = excluded.etag
    """, (endpoint_key, last_modified, etag))
    conn.commit()
    conn.close()


def get_trailing_metrics(item_id: int, window: int = 24):
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT min_unit_price, total_quantity
        FROM price_history
        WHERE item_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (item_id, window))
    rows = cursor.fetchall()
    conn.close()

    if len(rows) < 6:
        return None, None, None

    prices = [r[0] for r in rows]
    quantities = [r[1] for r in rows]
    n = len(quantities)

    price_sma = sum(prices) / n
    volume_sma = sum(quantities) / n

    variance = sum((q - volume_sma) ** 2 for q in quantities) / (n - 1)
    volume_std = math.sqrt(variance)

    return price_sma, volume_sma, volume_std


def get_cyclical_seasonal_baseline(item_id: int, current_timestamp_iso: str):
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT min_unit_price, total_quantity
        FROM price_history
        WHERE item_id = ?
          AND timestamp < ?
          AND strftime('%w', timestamp) = strftime('%w', ?)
          AND CAST(strftime('%H', timestamp) AS INTEGER) BETWEEN 
              (CAST(strftime('%H', ?) AS INTEGER) - 1) AND 
              (CAST(strftime('%H', ?) AS INTEGER) + 1)
        ORDER BY timestamp DESC
        LIMIT 16
    """, (item_id, current_timestamp_iso, current_timestamp_iso, current_timestamp_iso, current_timestamp_iso))

    rows = cursor.fetchall()
    conn.close()

    if len(rows) < 2:
        return None, None

    prices = [r[0] for r in rows]
    quantities = [r[1] for r in rows]

    return sum(prices) / len(prices), sum(quantities) / len(quantities)


def send_market_alert(
    item_id: int,
    item_name: str,
    unit_price: int,
    price_sma: float,
    current_qty: int,
    volume_sma: float,
    z_score: float,
    price_dip: bool,
    volume_surge: bool,
    breakdown: dict,
    cluster_desc: str,
    seasonal_price: float = None
):
    if not DISCORD_WEBHOOK_URL:
        return

    price_diff_pct = ((price_sma - unit_price) / price_sma) * 100
    qty_diff = current_qty - int(volume_sma)

    if price_dip and volume_surge:
        title = f"💥 Market Dump: {item_name}"
        color = 15158332
        desc = f"Heavy supply shock (**Z={z_score:+.2f}σ**) with a **{price_diff_pct:.1f}% discount** on {cluster_desc}."
    elif volume_surge:
        title = f"📦 Supply Surge: {item_name}"
        color = 3447003
        desc = f"Mega-realm inventory jumped **{z_score:+.2f}σ** above mean (**+{qty_diff} units**)."
    else:
        title = f"🎯 Value Buy Signal: {item_name}"
        color = 4840832
        desc = f"Spot price is **{price_diff_pct:.1f}% below** the 24-hour moving average."

    fields = [
        {"name": "Lowest Active Buyout", "value": f"**{format_copper(unit_price)}**", "inline": True},
        {"name": "24h Price Baseline", "value": f"{format_copper(int(price_sma))} ({price_diff_pct:+.1f}%)", "inline": True},
        {"name": "Active Units (Med + Long)", "value": f"**{current_qty}**", "inline": True},
        {"name": "Supply Z-Score", "value": f"**{z_score:+.2f}σ** ({qty_diff:+d})", "inline": True}
    ]

    if seasonal_price and seasonal_price > 0:
        seasonal_diff_pct = ((seasonal_price - unit_price) / seasonal_price) * 100
        fields.append({
            "name": "Day-of-Week Expected",
            "value": f"{format_copper(int(seasonal_price))} ({seasonal_diff_pct:+.1f}%)",
            "inline": True
        })

    fields.append({
        "name": "Full Expiration Spectrum",
        "value": (
            f"⏳ Short: `{breakdown['SHORT']}` | 🔹 Med: `{breakdown['MEDIUM']}`\n"
            f"🔷 Long: `{breakdown['LONG']}` | 📦 Very Long: `{breakdown['VERY_LONG']}`"
        ),
        "inline": False
    })

    payload = {
        "username": "AH Market Sentinel",
        "avatar_url": "https://render.worldofwarcraft.com/icons/56/inv_misc_coin_01.jpg",
        "embeds": [{
            "title": title,
            "description": desc,
            "color": color,
            "fields": fields,
            "footer": {"text": f"Cluster: {cluster_desc} • WoW: Forever Sentinel"},
            "timestamp": datetime.utcnow().isoformat()
        }]
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        res.raise_for_status()
        logging.info(f"Discord alert sent for {item_name} on {cluster_desc}.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to post Discord webhook: {e}")


def fetch_and_record(target_url: str, target_desc: str):
    watchlist = load_active_watchlist()
    if not watchlist:
        logging.info("No active items in watchlist. Skipping snapshot.")
        return

    token = get_oauth_token()
    cache_key = f"{REGION}:{target_url}"
    last_mod, etag = load_cached_headers(cache_key)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Encoding": "gzip"
    }
    if last_mod:
        headers["If-Modified-Since"] = last_mod
    if etag:
        headers["If-None-Match"] = etag

    params = {"namespace": NAMESPACE, "locale": "en_US"}

    logging.info(f"Checking auction snapshot on {target_desc}...")
    res = requests.get(target_url, headers=headers, params=params, stream=True, timeout=60)

    if res.status_code == 401:
        logging.warning("HTTP 401 Unauthorized. Refreshing token...")
        token = get_oauth_token(force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        res = requests.get(target_url, headers=headers, params=params, stream=True, timeout=60)

    with res:
        if res.status_code == 304:
            logging.info("HTTP 304 Not Modified: Snapshot unchanged. Skipping.")
            return

        res.raise_for_status()

        new_last_mod = res.headers.get("Last-Modified")
        new_etag = res.headers.get("ETag")
        if new_last_mod or new_etag:
            save_cached_headers(cache_key, new_last_mod, new_etag)

        logging.info("HTTP 200 OK: New dump received. Streaming items...")

        summary = {
            item_id: {
                "min_price": None,
                "active_quantity": 0,
                "buckets": {"SHORT": 0, "MEDIUM": 0, "LONG": 0, "VERY_LONG": 0}
            }
            for item_id in watchlist
        }

        auctions_stream = ijson.items(res.raw, "auctions.item")

        for auc in auctions_stream:
            item_data = auc.get("item")
            if not item_data:
                continue

            item_id = item_data.get("id")
            if item_id in watchlist:
                qty = auc.get("quantity", 1)

                # Schema Compatibility: Extract unit_price or calculate from buyout
                unit_price = auc.get("unit_price")
                if unit_price is None:
                    buyout = auc.get("buyout", 0)
                    unit_price = (buyout // qty) if qty > 0 else 0

                if unit_price <= 0:
                    continue

                # Fallback for commodities omitting duration timers
                time_left = auc.get("time_left")
                if not time_left or time_left == "UNKNOWN":
                    time_left = "LONG"

                if time_left in summary[item_id]["buckets"]:
                    summary[item_id]["buckets"][time_left] += qty

                if time_left in ACTIVE_DURATIONS:
                    summary[item_id]["active_quantity"] += qty
                    cur_min = summary[item_id]["min_price"]
                    if cur_min is None or unit_price < cur_min:
                        summary[item_id]["min_price"] = unit_price

    now_iso = datetime.utcnow().isoformat()
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()

    for item_id, conf in watchlist.items():
        name = conf["name"]
        min_surge_units = conf.get("min_surge_units", 1)
        min_p = summary[item_id]["min_price"]
        active_q = summary[item_id]["active_quantity"]
        buckets = summary[item_id]["buckets"]

        if min_p is not None:
            price_sma, volume_sma, volume_std = get_trailing_metrics(item_id, window=24)
            seasonal_price, seasonal_volume = get_cyclical_seasonal_baseline(item_id, now_iso)

            cursor.execute("""
                INSERT INTO price_history (
                    timestamp, item_id, item_name, min_unit_price, total_quantity,
                    qty_short, qty_medium, qty_long, qty_very_long
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_iso, item_id, name, min_p, active_q,
                buckets["SHORT"], buckets["MEDIUM"], buckets["LONG"], buckets["VERY_LONG"]
            ))

            if price_sma and volume_sma and volume_std is not None:
                price_discount_pct = ((price_sma - min_p) / price_sma) * 100
                qty_delta = active_q - volume_sma
                z_score = (qty_delta / volume_std) if volume_std > 0 else 0.0

                has_price_dip = price_discount_pct >= PRICE_DISCOUNT_PERCENT
                has_volume_surge = (z_score >= VOLUME_Z_SCORE_THRESHOLD) and (qty_delta >= min_surge_units)

                tracker = ACTIVE_ALERTS.setdefault(item_id, {"price_alerted": None, "volume_alerted": None})

                should_alert = False
                if has_price_dip and tracker["price_alerted"] != min_p:
                    should_alert = True
                if has_volume_surge and tracker["volume_alerted"] != active_q:
                    should_alert = True

                if should_alert:
                    send_market_alert(
                        item_id, name, min_p, price_sma, active_q, volume_sma,
                        z_score, has_price_dip, has_volume_surge, buckets,
                        cluster_desc=target_desc, seasonal_price=seasonal_price
                    )
                    tracker["price_alerted"] = min_p if has_price_dip else None
                    tracker["volume_alerted"] = active_q if has_volume_surge else None
                else:
                    if not has_price_dip:
                        tracker["price_alerted"] = None
                    if not has_volume_surge:
                        tracker["volume_alerted"] = None

    conn.commit()
    conn.close()

    # Record cluster state and health timestamp for web dashboard
    save_meta_val("last_successful_poll", now_iso)
    save_meta_val("current_region", REGION)
    save_meta_val("current_cluster", args.cluster)


def run_loop():
    init_db()
    prune_and_downsample()

    target_url, target_desc = resolve_market_target(force_refresh=args.refresh_cluster)
    logging.info(f"Sentinel online for [{target_desc}] on 15m polling interval.")

    poll_count = 0
    while True:
        try:
            fetch_and_record(target_url, target_desc)
            poll_count += 1

            if poll_count >= 96:
                prune_and_downsample()
                poll_count = 0

        except Exception as e:
            logging.error(f"Execution error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_loop()
