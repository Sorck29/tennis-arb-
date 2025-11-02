# app.py
import os
import time
import math
import requests
import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# 1) Config de página (DEBE ser el primer comando de Streamlit y solo una vez)
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Tennis Arbitrage (The Odds API)", layout="wide")

# ──────────────────────────────────────────────────────────────────────────────
# 2) Carga robusta de API key (Cloud → secrets, local → env var o archivo TOML)
# ──────────────────────────────────────────────────────────────────────────────
def load_api_key():
    # 1) Streamlit Cloud / local: st.secrets
    try:
        key = st.secrets.get("THE_ODDS_API_KEY")
        if key:
            return key
    except Exception:
        pass
    # 2) Variable de entorno
    key = os.getenv("THE_ODDS_API_KEY")
    if key:
        return key
    # 3) Leer .streamlit/secrets.toml manualmente (útil en local)
    try:
        # Python 3.11+: tomllib
        import tomllib  # si usas <3.11, instala 'toml' y ajusta este bloque
        with open(".streamlit/secrets.toml", "rb") as f:
            data = tomllib.load(f)
            key = data.get("THE_ODDS_API_KEY")
            if key:
                return key
    except Exception:
        pass
    return None

API_KEY = load_api_key()
BASE_URL = "https://api.the-odds-api.com/v4"
DEFAULT_SPORT = "tennis_atp"  # Cambia en la UI si lo deseas

# Diagnóstico útil
st.caption(f"📁 CWD: {os.getcwd()}")
st.caption(f"🔎 Existe .streamlit/secrets.toml? {os.path.exists('.streamlit/secrets.toml')}")
st.caption(f"🔐 API key cargada? {'sí' if bool(API_KEY) else 'no'}")

if not API_KEY:
    st.error("Falta THE_ODDS_API_KEY. En Cloud: Settings→Secrets. En local: .streamlit/secrets.toml o variable de entorno.")
    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# 3) Sidebar (controles)
# ──────────────────────────────────────────────────────────────────────────────
st.sidebar.title("⚙️ Configuración")
regions = st.sidebar.multiselect(
    "Regiones (bookmakers)",
    options=["uk", "eu", "us", "au"],
    default=["uk", "eu"],
    help="Filtra casas por región. The Odds API segmenta por regiones."
)
markets = ["h2h"]  # Tenis: trabajamos con H2H (dos resultados)
odds_format = "decimal"

sport_key = st.sidebar.text_input(
    "Clave de deporte (sport key)",
    value=DEFAULT_SPORT,
    help="Ejemplos: tennis, tennis_atp, tennis_wta, etc. Consulta /v4/sports para ver las claves disponibles."
)

min_edge = st.sidebar.slider(
    "Margen mínimo de arbitraje (%)",
    min_value=0.1, max_value=10.0, value=1.0, step=0.1,
    help="Solo se muestran oportunid. con edge >= este porcentaje."
)

bankroll = st.sidebar.number_input(
    "Bankroll para cálculo de stakes (€)",
    min_value=10.0, value=100.0, step=10.0
)

require_diff_books = st.sidebar.checkbox(
    "Exigir casas distintas para cada lado",
    value=True,
    help="Recomendado para arbitraje real entre casas."
)

ttl_seconds = st.sidebar.slider(
    "Cache TTL (segundos)",
    min_value=10, max_value=600, value=60, step=10,
    help="Cuánto tiempo cachéar los datos para evitar consumir cuota."
)

# ──────────────────────────────────────────────────────────────────────────────
# 4) Utils
#    - Cacheamos por argumentos y con un 'cache_buster' que cambia cada TTL.
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def fetch_odds(sport_key, regions, markets, odds_format, api_key, cache_buster):
    """
    Llama /v4/sports/{sport}/odds y devuelve JSON crudo + headers útiles.
    cache_buster: usa int(time.time() // ttl_seconds) para invalidar cache.
    """
    params = {
        "regions": ",".join(regions) if regions else "uk,eu",
        "markets": ",".join(markets),
        "oddsFormat": odds_format,
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
    """
    Busca arbitraje H2H en el bloque 'bookmakers' de un evento.
    Devuelve lista de dicts con oportunidades.
    """
    rows = []
    for bk in event_bookmakers:
        name = bk.get("title") or bk.get("key")
        for m in bk.get("markets", []):
            if m.get("key") != "h2h":
                continue
            outs = m.get("outcomes", [])
            if len(outs) != 2:
                continue
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
# 5) Main
# ──────────────────────────────────────────────────────────────────────────────
st.title("🎾 Tennis Arbitrage Finder — The Odds API")

with st.expander("ℹ️ Cómo funciona"):
    st.markdown("""
- Descargamos **cuotas H2H** (moneyline) de tenis desde **The Odds API**.
- Por cada partido, comprobamos todas las combinaciones de **dos casas** (A para Jugador1, B para Jugador2).
- Si `1/odd_A + 1/odd_B < 1`, hay **arbitraje**. Calculamos **stake óptimo** y **beneficio**.
- Puedes **filtrar por regiones** y **fijar el margen mínimo** que te interesa.
    """)

# Llamada con "cache_buster" basado en el TTL del sidebar
cache_buster = int(time.time() // ttl_seconds)

with st.spinner("Descargando cuotas…"):
    try:
        data, headers = fetch_odds(
            sport_key=sport_key,
            regions=regions,
            markets=markets,
            odds_format=odds_format,
            api_key=API_KEY,
            cache_buster=cache_buster
        )
    except requests.HTTPError as e:
        st.error(f"Error HTTP {e.response.status_code}: {e.response.text}")
        st.stop()
    except Exception as e:
        st.error(f"Error: {e}")
        st.stop()

# Mostrar info de cuota/uso
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
    title = f"{home} vs {away}" if home and away else ev.get("sport_title", "Tennis")

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
    st.info("No se encontraron arbitrajes con el umbral seleccionado. Prueba a bajar el margen mínimo o cambiar regiones.")
else:
    df = pd.DataFrame(all_rows).sort_values(by="edge_%", ascending=False)
    st.dataframe(df, use_container_width=True)

    # Descarga CSV
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("Descargar CSV", data=csv, file_name="tennis_arbs.csv", mime="text/csv")
