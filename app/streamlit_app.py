import streamlit as st
from streamlit_folium import st_folium
import asyncio
import time
import pandas as pd
from datetime import datetime, timezone, timedelta

from config import DB_CONFIG
from database import get_ship_positions, init_database
from map_utils import create_map

@st.cache_data(ttl=300) 
def check_db_initialized():
    """Check if database is initialized (cached)"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(init_database())
    loop.close()
    return result

st.set_page_config(
    page_title="Ship Tracker RT",
    page_icon="🚢",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("🚢 Ship Tracker RT - Real-Time Maritime Vessel Tracker")
st.markdown("---")

with st.sidebar:
    st.header("⚙️ Settings")
    
    auto_refresh = st.checkbox("Auto refresh", value=False, key="auto_refresh")
    
    refresh_interval = st.slider(
        label="Refresh interval (seconds)",
        min_value=3,
        max_value=60,
        value=10,
        step=1,
        key="refresh_interval",
        disabled=not auto_refresh,
        help="How often to automatically refresh the map"
    )
    
    max_age_minutes = st.slider(
        label="Max age (minutes)",
        min_value=1,
        max_value=60,
        value=30,
        step=1,
        key="max_age_minutes",
        help="Show only ships updated within this time"
    )
    
    st.markdown("---")
    st.info(f"**Current settings:**\n- Auto refresh: {'ON' if auto_refresh else 'OFF'}\n- Interval: {refresh_interval}s\n- Max age: {max_age_minutes}min")
    
    if st.button("🔄 Refresh now", use_container_width=True):
        st.rerun()


def main(refresh_interval: int, max_age_minutes: int, auto_refresh: bool):
    """Main application function"""

    if not check_db_initialized():
        st.error("Database not initialized. Check PostgreSQL logs.")
        return
    
    # Get data from database (with short cache)
    with st.spinner("Loading data..."):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ship_positions = loop.run_until_complete(get_ship_positions(max_age_minutes))
        loop.close()
    
    # Statistics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total ships", len(ship_positions))
    with col2:
        if ship_positions:
            active_ships = len([
                s for s in ship_positions 
                if s['updated_at'] > datetime.now(timezone.utc) - timedelta(minutes=5)
            ])
            st.metric("Active (5 min)", active_ships)
        else:
            st.metric("Active (5 min)", 0)
    with col3:
        if ship_positions:
            avg_speed = sum([
                float(s['speed_over_ground']) or 0 for s in ship_positions
            ]) / len(ship_positions)
            st.metric("Average speed", f"{avg_speed:.1f} knots")
        else:
            st.metric("Average speed", "0 knots")
    with col4:
        st.metric(
            "Last update", 
            datetime.now(timezone.utc).strftime("%H:%M:%S")
        )
    
    st.markdown("---")
    
    # Create and display map (without container to avoid darkening)
    if ship_positions:
        map_obj = create_map(ship_positions)
        st_folium(map_obj, width=None, height=600, key="ship_map")
    else:
        st.warning("⚠️ No ship data. Make sure the data collection script is running.")
        m = create_map([])
        st_folium(m, width=None, height=600, key="ship_map")
    
    # Data table
    if ship_positions:
        with st.expander("📊 Data table"):
            df_data = []
            for row in ship_positions:
                df_data.append({
                    'Ship ID': row['ship_id'],
                    'Latitude': f"{row['latitude']:.6f}",
                    'Longitude': f"{row['longitude']:.6f}",
                    'Course (°)': row['course_over_ground'] or 'N/A',
                    'Speed (knots)': row['speed_over_ground'] or 'N/A',
                    'Heading (°)': row['heading'] or 'N/A',
                    'Status': row['navigational_status'] or 'N/A',
                    'Updated': row['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if row['updated_at'] else 'N/A'
                })
            df = pd.DataFrame(df_data)
            st.dataframe(df, use_container_width=True)
    
    # Auto refresh only if enabled
    if auto_refresh:
        time.sleep(refresh_interval)
        st.rerun()


if __name__ == "__main__":
    # Call main with parameters from sidebar
    main(refresh_interval, max_age_minutes, auto_refresh)
