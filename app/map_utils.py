"""Utilities for working with maps"""
import folium
from typing import List, Dict


def create_map(ship_positions: List[Dict]) -> folium.Map:
    """
    Create Folium map with ship markers
    
    Args:
        ship_positions: List of dictionaries with ship position data
        
    Returns:
        Folium map object
    """
    if not ship_positions:
        # If no data, show default map (Singapore)
        m = folium.Map(
            location=[1.3521, 103.8198],
            zoom_start=10,
            tiles='OpenStreetMap'
        )
        return m
    
    # Calculate map center based on ship positions
    lats = [float(row['latitude']) for row in ship_positions]
    lons = [float(row['longitude']) for row in ship_positions]
    
    center_lat = sum(lats) / len(lats) if lats else 1.3521
    center_lon = sum(lons) / len(lons) if lons else 103.8198
    
    # Create map
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=10,
        tiles='OpenStreetMap'
    )
    
    # Add markers for each ship
    for row in ship_positions:
        ship_id = row['ship_id']
        lat = float(row['latitude'])
        lon = float(row['longitude'])
        cog = row['course_over_ground']
        sog = row['speed_over_ground']
        heading = row['heading']
        status = row['navigational_status']
        timestamp = row['timestamp']
        
        # Create popup with ship information
        popup_html = f"""
        <div style="font-family: Arial; min-width: 200px;">
            <h4>🚢 Судно ID: {ship_id}</h4>
            <hr>
            <p><b>Координаты:</b><br>
            Широта: {lat:.6f}<br>
            Долгота: {lon:.6f}</p>
            <p><b>Курс:</b> {cog if cog else 'N/A'}°<br>
            <b>Скорость:</b> {sog if sog else 'N/A'} узлов<br>
            <b>Направление:</b> {heading if heading else 'N/A'}°</p>
            <p><b>Статус:</b> {status if status else 'N/A'}</p>
            <p><b>Обновлено:</b><br>
            {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC') if timestamp else 'N/A'}</p>
        </div>
        """
        
        # Ship icon with direction consideration
        icon_color = 'blue'
        if heading:
            # Rotate icon according to movement direction
            icon = folium.Icon(
                icon='ship',
                prefix='fa',
                color=icon_color,
                icon_color='white'
            )
        else:
            icon = folium.Icon(
                icon='ship',
                prefix='fa',
                color=icon_color
            )
        
        # Add marker
        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"Судно {ship_id}",
            icon=icon
        ).add_to(m)
    
    return m

