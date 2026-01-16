import folium
from typing import List, Dict


def create_map(ship_positions: List[Dict]) -> folium.Map:
 
    if not ship_positions:
        m = folium.Map(
            location=[1.3521, 103.8198],
            zoom_start=10,
            tiles='OpenStreetMap'
        )
        return m
    
    lats = [float(row['latitude']) for row in ship_positions]
    lons = [float(row['longitude']) for row in ship_positions]
    
    center_lat = sum(lats) / len(lats) if lats else 1.3521
    center_lon = sum(lons) / len(lons) if lons else 103.8198
    
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=10,
        tiles='OpenStreetMap'
    )
    
    for row in ship_positions:
        ship_id = row['ship_id']
        lat = float(row['latitude'])
        lon = float(row['longitude'])
        cog = row['course_over_ground']
        sog = row['speed_over_ground']
        heading = row['heading']
        status = row['navigational_status']
        timestamp = row['timestamp']
        
        popup_html = f"""
        <div style="font-family: Arial; min-width: 200px;">
            <h4>🚢 Ship ID: {ship_id}</h4>
            <hr>
            <p><b>Coordinates:</b><br>
            Latitude: {lat:.6f}<br>
            Longitude: {lon:.6f}</p>
            <p><b>Course:</b> {cog if cog else 'N/A'}°<br>
            <b>Speed:</b> {sog if sog else 'N/A'} knots<br>
            <b>Direction:</b> {heading if heading else 'N/A'}°</p>
            <p><b>Status:</b> {status if status else 'N/A'}</p>
            <p><b>Updated:</b><br>
            {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC') if timestamp else 'N/A'}</p>
        </div>
        """
        
        icon_color = 'blue'
        if heading:
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
        
        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"Ship ID: {ship_id}",
            icon=icon
        ).add_to(m)
    
    return m

