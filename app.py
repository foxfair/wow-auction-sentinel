import math
import sqlite3
from typing import Optional
from datetime import datetime
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

app = FastAPI(title="WoW Auction Price & Duration Tracker")
DB_FILE = "market_data.db"
DISCOUNT_THRESHOLD_RATIO = 0.85
VOLUME_Z_SCORE_THRESHOLD = 2.0


class WatchlistItem(BaseModel):
    item_id: int
    name: str
    min_surge_units: Optional[int] = 1


def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/status")
def get_system_status():
    """Returns the current target cluster, region, and last poll time."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value, updated_at FROM market_meta")
    rows = cursor.fetchall()
    conn.close()

    meta = {row["key"]: row["value"] for row in rows}
    current_reg = meta.get("current_region", "us")
    current_clust = meta.get("current_cluster", "mega")
    cluster_desc = meta.get(f"target_desc:{current_reg}:{current_clust}", "Discovered Mega-Cluster")

    return {
        "status": "online",
        "region": current_reg.upper(),
        "cluster_target": cluster_desc,
        "endpoint": meta.get(f"target_url:{current_reg}:{current_clust}", "Active"),
        "last_updated": meta.get("last_successful_poll")
    }


@app.get("/api/items")
def list_tracked_items():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT item_id, item_name 
        FROM price_history 
        ORDER BY item_name ASC
    """)
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


@app.get("/api/watchlist")
def get_watchlist():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT item_id, name, min_surge_units, is_active, created_at
        FROM watchlist
        ORDER BY is_active DESC, name ASC
    """)
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


@app.post("/api/watchlist")
def upsert_watchlist_item(item: WatchlistItem):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_iso = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO watchlist (item_id, name, min_surge_units, is_active, created_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            name = excluded.name,
            min_surge_units = excluded.min_surge_units,
            is_active = 1
    """, (item.item_id, item.name.strip(), item.min_surge_units or 1, now_iso))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Saved {item.name} (ID: {item.item_id})"}


@app.patch("/api/watchlist/{item_id}/toggle")
def toggle_watchlist_item(item_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_active FROM watchlist WHERE item_id = ?", (item_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Item not found in watchlist")

    new_state = 0 if row["is_active"] == 1 else 1
    cursor.execute("UPDATE watchlist SET is_active = ? WHERE item_id = ?", (new_state, item_id))
    conn.commit()
    conn.close()
    return {"status": "success", "item_id": item_id, "is_active": new_state}


@app.delete("/api/watchlist/{item_id}")
def delete_watchlist_item(item_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlist WHERE item_id = ?", (item_id,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"status": "success", "message": f"Item {item_id} removed from watchlist"}


@app.get("/api/history/{item_id}")
def get_price_history(item_id: int, limit: Optional[int] = 168):
    conn = get_db_connection()
    cursor = conn.cursor()
    fetch_limit = limit + 24
    cursor.execute("""
        SELECT 
            timestamp, item_id, item_name, min_unit_price, total_quantity,
            COALESCE(qty_short, 0) as qty_short,
            COALESCE(qty_medium, 0) as qty_medium,
            COALESCE(qty_long, 0) as qty_long,
            COALESCE(qty_very_long, 0) as qty_very_long
        FROM price_history
        WHERE item_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (item_id, fetch_limit))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="Item not found")

    chronological = list(reversed(rows))
    computed_records = []
    
    price_window = []
    volume_window = []

    for row in chronological:
        gold_val = round(row["min_unit_price"] / 10000.0, 2)
        total_q = row["total_quantity"]

        price_window.append(gold_val)
        volume_window.append(total_q)

        if len(price_window) > 24:
            price_window.pop(0)
        if len(volume_window) > 24:
            volume_window.pop(0)

        sma_24 = round(sum(price_window) / len(price_window), 2) if len(price_window) >= 3 else None
        
        is_buy_signal = False
        discount_pct = 0.0
        if sma_24 and sma_24 > 0 and gold_val <= (sma_24 * DISCOUNT_THRESHOLD_RATIO):
            is_buy_signal = True
            discount_pct = round(((sma_24 - gold_val) / sma_24) * 100, 1)

        n_vol = len(volume_window)
        vol_sma_24 = round(sum(volume_window) / n_vol, 1) if n_vol >= 3 else None
        vol_z_score = 0.0
        vol_std = 0.0
        is_volume_anomaly = False

        if n_vol >= 6:
            variance = sum((q - vol_sma_24) ** 2 for q in volume_window) / (n_vol - 1)
            vol_std = round(math.sqrt(variance), 2)
            if vol_std > 0:
                vol_z_score = round((total_q - vol_sma_24) / vol_std, 2)
            
            if vol_z_score >= VOLUME_Z_SCORE_THRESHOLD:
                is_volume_anomaly = True

        computed_records.append({
            "timestamp": row["timestamp"],
            "item_name": row["item_name"],
            "gold_price": gold_val,
            "sma_24": sma_24,
            "is_buy_signal": is_buy_signal,
            "discount_pct": discount_pct,
            "total_quantity": total_q,
            "vol_sma_24": vol_sma_24,
            "vol_std": vol_std,
            "vol_z_score": vol_z_score,
            "is_volume_anomaly": is_volume_anomaly,
            "qty_short": row["qty_short"],
            "qty_medium": row["qty_medium"],
            "qty_long": row["qty_long"],
            "qty_very_long": row["qty_very_long"]
        })

    return computed_records[-limit:]


@app.get("/", response_class=HTMLResponse)
def index_page():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>WoW: Forever Market Sentinel</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f1218;
      color: #e2e8f0;
      margin: 0;
      padding: 24px;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      max-width: 1100px;
      margin: 0 auto 16px auto;
    }
    .badge-container {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 4px;
    }
    .cluster-badge {
      font-size: 12px;
      color: #38bdf8;
      background: rgba(56, 189, 248, 0.12);
      border: 1px solid rgba(56, 189, 248, 0.25);
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 500;
    }
    .sync-status {
      font-size: 11px;
      color: #94a3b8;
    }
    .controls {
      display: flex;
      gap: 12px;
      align-items: center;
    }
    select, input {
      background: #1b202c;
      color: #ffd100;
      border: 1px solid #334155;
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 14px;
      outline: none;
    }
    .btn-action {
      background: #2563eb;
      color: #f8fafc;
      border: none;
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
    }
    .btn-action:hover {
      background: #1d4ed8;
    }
    .btn-group {
      display: flex;
      background: #1b202c;
      border: 1px solid #334155;
      border-radius: 6px;
      overflow: hidden;
    }
    .btn-range {
      background: transparent;
      border: none;
      color: #94a3b8;
      padding: 8px 14px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn-range:hover {
      color: #f8fafc;
      background: rgba(255, 255, 255, 0.05);
    }
    .btn-range.active {
      background: #ffd100;
      color: #0f1218;
      font-weight: 600;
    }
    .stats-bar {
      display: flex;
      gap: 12px;
      max-width: 1100px;
      margin: 0 auto 16px auto;
    }
    .card {
      background: #1b202c;
      border: 1px solid #2d3748;
      border-radius: 8px;
      padding: 12px 14px;
      flex: 1;
    }
    .card-label {
      font-size: 11px;
      color: #94a3b8;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .card-val {
      font-size: 18px;
      font-weight: bold;
      margin-top: 4px;
      color: #f8fafc;
    }
    .container {
      max-width: 1100px;
      margin: 0 auto 20px auto;
      background: #1b202c;
      border-radius: 8px;
      padding: 20px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    }
    .chart-title {
      font-size: 14px;
      font-weight: 600;
      color: #cbd5e1;
      margin-bottom: 12px;
    }
    canvas {
      width: 100% !important;
      max-height: 320px;
    }
    .modal-backdrop {
      display: none;
      position: fixed;
      top: 0; left: 0; width: 100%; height: 100%;
      background: rgba(0, 0, 0, 0.7);
      backdrop-filter: blur(4px);
      z-index: 100;
      justify-content: center;
      align-items: center;
    }
    .modal {
      background: #151922;
      border: 1px solid #334155;
      border-radius: 10px;
      width: 580px;
      max-width: 90%;
      padding: 24px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }
    .modal-close {
      background: none;
      border: none;
      color: #94a3b8;
      font-size: 20px;
      cursor: pointer;
    }
    .watchlist-table {
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 16px;
    }
    .watchlist-table th, .watchlist-table td {
      padding: 8px 12px;
      text-align: left;
      font-size: 13px;
      border-bottom: 1px solid #2d3748;
    }
    .btn-sm {
      padding: 4px 8px;
      font-size: 12px;
      border-radius: 4px;
      border: none;
      cursor: pointer;
    }
    .btn-toggle { background: #334155; color: #f8fafc; }
    .btn-delete { background: #ef4444; color: #fff; margin-left: 4px; }
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h2 style="margin: 0 0 2px 0;">WoW: Forever Market Sentinel</h2>
      <div class="badge-container">
        <span id="clusterBadge" class="cluster-badge">🌐 Discovering Cluster...</span>
        <span id="syncStatus" class="sync-status">Checking sync...</span>
      </div>
    </div>
    <div class="controls">
      <div>
        <label for="itemSelect" style="font-size: 13px; color: #94a3b8; margin-right: 4px;">Reagent:</label>
        <select id="itemSelect"></select>
      </div>
      <button class="btn-action" onclick="openWatchlistModal()">⚙️ Watchlist</button>
      <div class="btn-group">
        <button class="btn-range" data-limit="24">24h</button>
        <button class="btn-range" data-limit="72">3d</button>
        <button class="btn-range active" data-limit="168">7d</button>
        <button class="btn-range" data-limit="336">14d</button>
      </div>
    </div>
  </div>

  <div class="stats-bar">
    <div class="card">
      <div class="card-label">Lowest Unit Price</div>
      <div class="card-val" id="statPrice">--</div>
    </div>
    <div class="card">
      <div class="card-label">24h Price Baseline</div>
      <div class="card-val" id="statSMA">--</div>
    </div>
    <div class="card">
      <div class="card-label">Active Stock (Med+Long)</div>
      <div class="card-val" id="statQty">--</div>
    </div>
    <div class="card">
      <div class="card-label">Volume Anomaly (Z-Score)</div>
      <div class="card-val" id="statZScore">--</div>
    </div>
    <div class="card">
      <div class="card-label">Market Status</div>
      <div class="card-val" id="statSignal">--</div>
    </div>
  </div>

  <div class="container">
    <div class="chart-title">Spot Buyout & 24h Moving Average Trend</div>
    <canvas id="priceChart"></canvas>
  </div>

  <div class="container">
    <div class="chart-title">Auction Duration Breakdown (Stacked Area)</div>
    <canvas id="durationChart"></canvas>
  </div>

  <!-- Watchlist Modal -->
  <div class="modal-backdrop" id="modalBackdrop">
    <div class="modal">
      <div class="modal-header">
        <h3 style="margin: 0;">Manage Watchlist</h3>
        <button class="modal-close" onclick="closeWatchlistModal()">&times;</button>
      </div>
      <table class="watchlist-table">
        <thead>
          <tr style="color: #94a3b8;">
            <th>ID</th>
            <th>Name</th>
            <th>Min Surge</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="watchlistTableBody"></tbody>
      </table>

      <form id="addWatchlistForm" onsubmit="addWatchlistItem(event)" style="display: flex; gap: 8px; flex-wrap: wrap;">
        <input type="number" id="addItemId" placeholder="Item ID (e.g. 13468)" required style="width: 120px;">
        <input type="text" id="addItemName" placeholder="Item Name" required style="flex: 1;">
        <input type="number" id="addMinSurge" placeholder="Min Surge" value="5" style="width: 90px;">
        <button type="submit" class="btn-action">+ Add / Save</button>
      </form>
    </div>
  </div>

  <script>
    let priceChartInstance = null;
    let durationChartInstance = null;
    let activeLimit = 168;

    async function updateClusterStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        if (data.cluster_target) {
          document.getElementById('clusterBadge').textContent = `🌐 ${data.cluster_target}`;
        }
        if (data.last_updated) {
          const d = new Date(data.last_updated);
          document.getElementById('syncStatus').textContent = `Synced: ${d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}`;
        } else {
          document.getElementById('syncStatus').textContent = 'Awaiting initial snapshot...';
        }
      } catch (e) {
        document.getElementById('clusterBadge').textContent = '🌐 Mega-Cluster';
      }
    }

    async function loadItems() {
      const res = await fetch('/api/items');
      const items = await res.json();
      const selector = document.getElementById('itemSelect');
      selector.innerHTML = '';

      if (items.length === 0) {
        selector.innerHTML = '<option>No tracked items yet</option>';
        return;
      }

      items.forEach((item) => {
        const opt = document.createElement('option');
        opt.value = item.item_id;
        opt.textContent = `${item.item_name} (ID: ${item.item_id})`;
        selector.appendChild(opt);
      });

      renderDashboard(items[0].item_id, activeLimit);
      selector.addEventListener('change', (e) => renderDashboard(e.target.value, activeLimit));

      document.querySelectorAll('.btn-range').forEach(btn => {
        btn.addEventListener('click', (e) => {
          document.querySelectorAll('.btn-range').forEach(b => b.classList.remove('active'));
          e.target.classList.add('active');
          activeLimit = parseInt(e.target.getAttribute('data-limit'));
          renderDashboard(selector.value, activeLimit);
        });
      });
    }

    async function renderDashboard(itemId, limit) {
      const res = await fetch(`/api/history/${itemId}?limit=${limit}`);
      if (!res.ok) return;
      const history = await res.json();
      if (history.length === 0) return;

      const latest = history[history.length - 1];
      document.getElementById('statPrice').textContent = `${latest.gold_price}g`;
      document.getElementById('statSMA').textContent = latest.sma_24 ? `${latest.sma_24}g` : 'Calculating...';
      document.getElementById('statQty').textContent = `${latest.total_quantity} units`;

      const zScoreElem = document.getElementById('statZScore');
      const zVal = latest.vol_z_score;
      if (latest.is_volume_anomaly) {
        zScoreElem.textContent = `🚨 +${zVal}σ (Dump)`;
        zScoreElem.style.color = '#f87171';
      } else {
        zScoreElem.textContent = `${zVal >= 0 ? '+' : ''}${zVal}σ (±${latest.vol_std})`;
        zScoreElem.style.color = '#38bdf8';
      }

      const signalElem = document.getElementById('statSignal');
      if (latest.is_buy_signal && latest.is_volume_anomaly) {
        signalElem.textContent = `💥 DUMP DIP (-${latest.discount_pct}%)`;
        signalElem.style.color = '#f87171';
      } else if (latest.is_buy_signal) {
        signalElem.textContent = `🎯 BUY (-${latest.discount_pct}%)`;
        signalElem.style.color = '#4ade80';
      } else if (latest.is_volume_anomaly) {
        signalElem.textContent = `📦 SUPPLY SURGE`;
        signalElem.style.color = '#38bdf8';
      } else {
        signalElem.textContent = 'NEUTRAL';
        signalElem.style.color = '#94a3b8';
      }

      const labels = history.map(row => {
        const d = new Date(row.timestamp);
        return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`;
      });

      const pricePoints = history.map(row => row.gold_price);
      const smaPoints = history.map(row => row.sma_24);
      const pointColors = history.map(row => row.is_buy_signal ? '#4ade80' : '#ffd100');
      const pointRadii = history.map(row => row.is_buy_signal ? 7 : 2);
      const pointStyles = history.map(row => row.is_buy_signal ? 'rectRot' : 'circle');

      const shortPoints = history.map(row => row.qty_short);
      const medPoints = history.map(row => row.qty_medium);
      const longPoints = history.map(row => row.qty_long);
      const veryLongPoints = history.map(row => row.qty_very_long);

      if (priceChartInstance) priceChartInstance.destroy();
      const ctxPrice = document.getElementById('priceChart').getContext('2d');
      priceChartInstance = new Chart(ctxPrice, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Spot Buyout (Gold)',
              data: pricePoints,
              borderColor: '#ffd100',
              backgroundColor: 'rgba(255, 209, 0, 0.05)',
              pointBackgroundColor: pointColors,
              pointBorderColor: pointColors,
              pointRadius: pointRadii,
              pointStyle: pointStyles,
              tension: 0.2,
              fill: true
            },
            {
              label: '24h SMA Baseline',
              data: smaPoints,
              borderColor: '#c084fc',
              backgroundColor: 'transparent',
              borderWidth: 2,
              borderDash: [4, 4],
              pointRadius: 0,
              tension: 0.3
            }
          ]
        },
        options: {
          responsive: true,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: '#e2e8f0' } }
          },
          scales: {
            x: { grid: { color: '#2d3748' }, ticks: { color: '#94a3b8' } },
            y: {
              grid: { color: '#2d3748' },
              ticks: { color: '#ffd100' },
              title: { display: true, text: 'Gold (g)', color: '#ffd100' }
            }
          }
        }
      });

      if (durationChartInstance) durationChartInstance.destroy();
      const ctxDuration = document.getElementById('durationChart').getContext('2d');
      durationChartInstance = new Chart(ctxDuration, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Short (<30m)',
              data: shortPoints,
              borderColor: '#f87171',
              backgroundColor: 'rgba(248, 113, 113, 0.65)',
              fill: true,
              tension: 0.3,
              pointRadius: 0
            },
            {
              label: 'Medium (30m–2h)',
              data: medPoints,
              borderColor: '#fbbf24',
              backgroundColor: 'rgba(251, 191, 36, 0.65)',
              fill: true,
              tension: 0.3,
              pointRadius: 0
            },
            {
              label: 'Long (2h–12h)',
              data: longPoints,
              borderColor: '#38bdf8',
              backgroundColor: 'rgba(56, 189, 248, 0.65)',
              fill: true,
              tension: 0.3,
              pointRadius: 0
            },
            {
              label: 'Very Long (12h–48h)',
              data: veryLongPoints,
              borderColor: '#a855f7',
              backgroundColor: 'rgba(168, 85, 247, 0.65)',
              fill: true,
              tension: 0.3,
              pointRadius: 0
            }
          ]
        },
        options: {
          responsive: true,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: '#e2e8f0' } },
            tooltip: {
              callbacks: {
                footer: (items) => {
                  const total = items.reduce((sum, item) => sum + item.parsed.y, 0);
                  return `Total AH Inventory: ${total} units`;
                }
              }
            }
          },
          scales: {
            x: { grid: { color: '#2d3748' }, ticks: { color: '#94a3b8' } },
            y: {
              stacked: true,
              grid: { color: '#2d3748' },
              ticks: { color: '#94a3b8' },
              title: { display: true, text: 'Total Units (Stacked)', color: '#94a3b8' }
            }
          }
        }
      });
    }

    async function openWatchlistModal() {
      document.getElementById('modalBackdrop').style.display = 'flex';
      await refreshWatchlistTable();
    }

    function closeWatchlistModal() {
      document.getElementById('modalBackdrop').style.display = 'none';
      loadItems();
    }

    async function refreshWatchlistTable() {
      const res = await fetch('/api/watchlist');
      const items = await res.json();
      const tbody = document.getElementById('watchlistTableBody');
      tbody.innerHTML = '';

      items.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${item.item_id}</td>
          <td><strong>${item.name}</strong></td>
          <td>+${item.min_surge_units}</td>
          <td>${item.is_active ? '<span style="color: #4ade80;">Active</span>' : '<span style="color: #94a3b8;">Paused</span>'}</td>
          <td>
            <button class="btn-sm btn-toggle" onclick="toggleWatchlist(${item.item_id})">${item.is_active ? 'Pause' : 'Resume'}</button>
            <button class="btn-sm btn-delete" onclick="deleteWatchlist(${item.item_id})">Delete</button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function addWatchlistItem(e) {
      e.preventDefault();
      const item_id = parseInt(document.getElementById('addItemId').value);
      const name = document.getElementById('addItemName').value;
      const min_surge_units = parseInt(document.getElementById('addMinSurge').value) || 1;

      await fetch('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id, name, min_surge_units })
      });

      document.getElementById('addItemId').value = '';
      document.getElementById('addItemName').value = '';
      document.getElementById('addMinSurge').value = '5';
      await refreshWatchlistTable();
    }

    async function toggleWatchlist(itemId) {
      await fetch(`/api/watchlist/${itemId}/toggle`, { method: 'PATCH' });
      await refreshWatchlistTable();
    }

    async function deleteWatchlist(itemId) {
      if (!confirm(`Are you sure you want to stop tracking item ${itemId}?`)) return;
      await fetch(`/api/watchlist/${itemId}`, { method: 'DELETE' });
      await refreshWatchlistTable();
    }

    updateClusterStatus();
    loadItems();
  </script>
</body>
</html>
    """
