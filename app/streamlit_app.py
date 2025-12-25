import streamlit as st
from streamlit_folium import st_folium
import asyncio
import time
import pandas as pd
from datetime import datetime, timezone, timedelta

from config import DB_CONFIG
from database import get_ship_positions, init_database
from map_utils import create_map

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
    
    refresh_interval = st.slider(
        min_value=1,
        max_value=60,
        value=5,
        step=1
    )
    
    max_age_minutes = st.slider(
        min_value=1,
        max_value=60,
        value=30,
        step=1
    )
    
    # Refresh button
    auto_refresh = st.checkbox("Auto refresh", value=True)
    
    if st.button("🔄 Refresh now"):
        st.rerun()


def main():
    """Main application function"""
    # Get data from database
    with st.spinner("Loading data..."):
        # Run asynchronous function
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Check database initialization
        db_initialized = loop.run_until_complete(init_database())
        if not db_initialized:
            st.error("Database not initialized. Check PostgreSQL logs.")
            loop.close()
            return
        
        # Get ship positions
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
    
    # Create and display map
    if ship_positions:
        map_obj = create_map(ship_positions)
        st_folium(map_obj, width=None, height=600)
        
        # Data table
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
    else:
        st.warning("⚠️ No ship data. Make sure the data collection script is running.")
        # Show empty map
        m = create_map([])
        st_folium(m, width=None, height=600)
    
    # Auto refresh
    if auto_refresh:
        time.sleep(refresh_interval)
        st.rerun()


if __name__ == "__main__":
    main()
