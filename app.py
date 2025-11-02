# app.py
import os
import time
import requests
import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# 1) Config de página (debe ser el primer st.* y solo una vez)
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Arbitrage Finder — The Odds API", layout="wide")

# ──────────────────────────────────────────────────────────────────────────────
# 2) Carga robusta de API key
# ──────────────────────────────────────────────────────────────────────────────
def load_api_key():
    try:
        key = st.secrets.get("THE_ODDS_API_KEY")
        if key:
            return key
    except Exception:
        pass
    key = os.getenv("THE_ODDS_API_KEY")
    if key:
        return key
    try:
        import tomllib  # Python 3.11+
        with open(".streamlit/secrets.toml", "rb") as f:
            data = tomllib.load(f)
            return data.get("THE_ODDS_API_KEY")
    except Exception:
        pass
    return None

API_KEY = load_api_key()
BASE_URL = "https://api.the-odds-api.com/v4"

# Diagnóstico útil
st.caption(f"📁 CWD: {os.getcwd()}")
st.caption(f"🔎 Existe .streamlit/secrets.toml? {os.path.exists('.streamlit/secrets.toml')}")
st.caption(f"🔐 API key cargada? {'sí' if bool(API_KEY) else 'no'}")

if not API_KEY:
    st.error("Falta THE_ODDS_API_KEY. En Cloud: Settings→Secrets. En local: .streamlit/secrets.toml o variable de entorno.")
    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# 3) Sidebar: configuración + selector de deporte con buscador
# ──────────────────────────────────────────────────────────────────────────────
st.sidebar.title("⚙️ Configuración")

regions = st.sidebar.multiselect(
    "Regiones (bookmakers)",
    options=["uk", "eu", "us", "au"],
    default=["uk", "eu"],
    help="Filtra casas por región (afecta qué casas devuelve la API)."
)

min_edge = st.sidebar.slider("Margen mínimo de arbitraje (%)", 0.1, 10.0, 1.0, 0.1)
bankroll = st.sidebar.number_input("Bankroll para cálculo de stakes (€)", min_value=10.0, value=100.0, step=10.0)
require_diff_books = st.sidebar.checkbox("Exigir casas distintas para cada lado", value=True)
ttl_seconds = st.sidebar.slider("Cache TTL (segundos)", 10, 600, 60, 10)
only_two_outcome_sports = st.sidebar.checkbox("Mostrar solo deportes H2H de 2 resultados", value=True, help="Recomendado para esta versión de arbitraje.")

sport_search = st.sidebar.text_input("Buscar deporte/torneo", value="", help="Filtra por texto en título o clave (ej. 'tennis', 'nba', 'mlb', 'wta').")

# ──────────────────────────────────────────────────────────────────────────────
# 4) Fetch helpers
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def fetch_sports(api_key, cache_buster):
    r = requests.get(f"{BASE_URL}/sports", params={"apiKey": api_key}, timeout=30)
    r.raise_for_status()
    return r.json()

@st.cache_data(show_spinner=False)
def fetch_odds(sport_key, regions, api_key, cache_buster):
    params = {
        "regions": ",".join(regions) if regions else "uk,eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "apiKey": api_key
    }
    url = f"{BASE_URL}/sports/{sport_key}/odds"
    r = requests.get(url, params=params, timeout=30)
    headers = {
        "x-requests-remaining": r.headers.get("x-requests-remaining"),
        "x-requests-used": r.headers.get("x-requests-used"),
        "x-requests-last": r.headers.get("x-requests-last"),
    }
    r.raise_for_status()
    return r.json(), headers

def best_two_outcome_arbs(event_bookmakers, require_diff_books=True):
    rows = []
    for bk in event_bookmakers:
        name = bk.get("title") or bk.get("key")
        for m in bk.get("markets", []):
            if m.get("key") != "h2h":
                continue
            outs = m.get("outcomes", [])
            if len(outs) != 2:
                continue  # ignoramos mercados con 3 resultados (ej. fútbol 1X2)
            o1, o2 = outs[0], outs[1]
            rows.append({
                "bookmaker": name,
                "outcome1_name": o1.get("name"),
                "outcome1_price": float(o1.get("price")) if o1.get("price") is not None else None,
                "outcome2_name": o2.get("name"),
                "outcome2_price": float(o2.get("price")) if o2.get("price") is not None else None,
            })

    arbs = []
    for i in range(len(rows)):
        for j in range(len(rows)):
            if i == j and require_diff_books:
                continue
            a, b = rows[i], rows[j]
            if not a["outcome1_price"] or not b["outcome2_price"]:
                continue
            inv_sum = (1.0 / a["outcome1_price"]) + (1.0 / b["outcome2_price"])
            if inv_sum < 1.0:
                edge = 1.0 - inv_sum
                arbs.append({
                    "bk_outcome1": a["bookmaker"],
                    "bk_outcome2": b["bookmaker"],
                    "outcome1": a["outcome1_name"],
                    "odd1": a["outcome1_price"],
                    "outcome2": b["outcome2_name"],
                    "odd2": b["outcome2_price"],
                    "edge": edge
                })
    arbs.sort(key=lambda d: d["edge"], reverse=True)
    return arbs

def stake_split(odd1, odd2, bankroll):
    inv1, inv2 = 1.0/odd1, 1.0/odd2
    denom = inv1 + inv2
    s1 = bankroll * (inv1 / denom)
    s2 = bankroll * (inv2 / denom)
    edge = 1.0 - denom
    profit = bankroll * edge
    return s1, s2, edge, profit

# ──────────────────────────────────────────────────────────────────────────────
# 5) UI principal
# ──────────────────────────────────────────────────────────────────────────────
st.title("🔎 Arbitrage Finder — The Odds API (H2H 2-way)")

with st.spinner("Cargando lista de deportes/torneos…"):
    cache_buster_sports = int(time.time() // (ttl_seconds or 60))
    all_sports = fetch_sports(API_KEY, cache_buster_sports)

# Filtrado por texto
def match_text(s, q):
    q = q.strip().lower()
    if not q:
        return True
    return q in (s.get("title","").lower()) or q in (s.get("key","").lower())

sports_filtered = [s for s in all_sports if match_text(s, sport_search)]

# (Opcional) lista de deportes con h2h de 2 resultados (heurística simple por nombre)
two_way_hint_keywords = ["tennis", "basket", "nba", "ufc", "mma", "boxing", "nhl", "mlb", "nfl", "table_tennis", "volleyball", "darts"]
if only_two_outcome_sports:
    sports_filtered = [s for s in sports_filtered if any(k in (s.get("key","").lower()) for k in two_way_hint_keywords)]

if not sports_filtered:
    st.warning("No se encontraron deportes con el filtro actual. Borra el texto de búsqueda o desmarca 'solo 2 resultados'.")
    st.stop()

sport_titles = [s.get("title") or s.get("key") for s in sports_filtered]
sport_keys   = [s.get("key") for s in sports_filtered]
selected_idx = 0
selected_title = st.sidebar.selectbox("Deporte / Torneo", sport_titles, index=selected_idx)
sport_key = sport_keys[sport_titles.index(selected_title)]

with st.expander("ℹ️ Cómo funciona"):
    st.markdown("""
- Descargamos **cuotas H2H** (moneyline) del **deporte/torneo** elegido.
- Probamos todas las combinaciones de **dos casas** y verificamos si `1/odd_A + 1/odd_B < 1`.
- Mostramos margen, stakes óptimos y beneficio para tu bankroll.
> Nota: esta versión solo busca arbitrajes en **mercados H2H de 2 resultados**.
    """)

# ──────────────────────────────────────────────────────────────────────────────
# 6) Descargar cuotas y detectar arbitrajes
# ──────────────────────────────────────────────────────────────────────────────
cache_buster_odds = int(time.time() // (ttl_seconds or 60))
with st.spinner(f"Descargando cuotas de: {selected_title}…"):
    try:
        data, headers = fetch_odds(sport_key, regions, API_KEY, cache_buster_odds)
    except requests.HTTPError as e:
        st.error(f"Error HTTP {e.response.status_code}: {e.response.text}")
        st.stop()
    except Exception as e:
        st.error(f"Error: {e}")
        st.stop()

# Métricas de uso de API
colh1, colh2, colh3 = st.columns(3)
colh1.metric("x-requests-remaining", headers.get("x-requests-remaining"))
colh2.metric("x-requests-used", headers.get("x-requests-used"))
colh3.metric("x-requests-last", headers.get("x-requests-last"))

# Parseo y detección
events = data if isinstance(data, list) else []
all_rows = []
for ev in events:
    event_id = ev.get("id")
    commence = ev.get("commence_time")
    home = (ev.get("home_team") or "").strip()
    away = (ev.get("away_team") or "").strip()
    title = f"{home} vs {away}" if home and away else (ev.get("sport_title") or selected_title)

    bks = ev.get("bookmakers", [])
    arbs = best_two_outcome_arbs(bks, require_diff_books=require_diff_books)
    arbs = [a for a in arbs if (a["edge"] * 100) >= min_edge]

    for a in arbs:
        s1, s2, edge, profit = stake_split(a["odd1"], a["odd2"], bankroll)
        all_rows.append({
            "event_id": event_id,
            "match": title,
            "start_time": commence,
            "bk_outcome1": a["bk_outcome1"],
            "outcome1": a["outcome1"],
            "odd1": a["odd1"],
            "bk_outcome2": a["bk_outcome2"],
            "outcome2": a["outcome2"],
            "odd2": a["odd2"],
            "edge_%": round(edge * 100, 3),
            "stake1": round(s1, 2),
            "stake2": round(s2, 2),
            "profit_€": round(profit, 2)
        })

# Resultados
st.subheader("💡 Oportunidades de arbitraje")
if not all_rows:
    st.info("No se encontraron arbitrajes H2H de 2 resultados con el umbral seleccionado. Prueba otro deporte/torneo, cambia regiones o baja el umbral.")
else:
    df = pd.DataFrame(all_rows).sort_values(by="edge_%", ascending=False)
    st.dataframe(df, use_container_width=True)
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("Descargar CSV", data=csv, file_name="arbs.csv", mime="text/csv")
