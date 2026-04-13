import os
import json
import ee
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Debug: Print environment variables (remove after debugging)
print("=== DEBUGGING EARTH ENGINE AUTH ===")
print(f"EE_ACCOUNT exists: {bool(os.environ.get('EE_ACCOUNT'))}")
print(f"EARTH_ENGINE_KEY exists: {bool(os.environ.get('EARTH_ENGINE_KEY'))}")

# Initialize Earth Engine
def init_earth_engine():
    try:
        # Check if we have service account credentials
        ee_account = os.environ.get('EE_ACCOUNT')
        ee_key = os.environ.get('EARTH_ENGINE_KEY')
        
        if ee_account and ee_key:
            print("Using Service Account Authentication...")
            
            # IMPORTANT: ee.ServiceAccountCredentials expects the JSON as a STRING, not a dict
            # If ee_key is already a string, use it directly
            if isinstance(ee_key, str):
                print("EARTH_ENGINE_KEY is a string, using directly")
                # The key_data parameter expects the JSON as a string
                credentials = ee.ServiceAccountCredentials(ee_account, key_data=ee_key)
                print("Successfully created credentials with string")
            else:
                # If it's a dict, convert back to string
                print("EARTH_ENGINE_KEY is a dict, converting to string")
                key_string = json.dumps(ee_key)
                credentials = ee.ServiceAccountCredentials(ee_account, key_data=key_string)
                print("Successfully created credentials after conversion")
            
            ee.Initialize(credentials)
            print("SUCCESS: Earth Engine Initialized with Service Account")
            return True
        else:
            print("Using Default Authentication (Local)...")
            ee.Initialize(project='ee-anusharacharla844')
            print("SUCCESS: Earth Engine Initialized Locally")
            return True
    except Exception as e:
        print(f"ERROR initializing Earth Engine: {e}")
        print(f"Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        return False

# Initialize on startup
init_earth_engine()

# Parameter Configuration Legend
PARAM_CONFIG = {
    "Chlorophyll-a": {
        "min": 0, 
        "max": 30, 
        "palette": ["#90CAF9", "#4CAF50", "#B71C1C"],
        "unit": "µg/L", 
        "th": [5, 15]
    },
    "Turbidity": {
        "min": 0, 
        "max": 20, 
        "palette": ["#E6CCB2", "#B08968", "#7F5539"],
        "unit": "NTU", 
        "th": [2, 10]
    },
    "Secchi Depth": {
        "min": 0, 
        "max": 5, 
        "palette": ["#1B5E20", "#42A5F5", "#B3E5FC"],
        "unit": "m", 
        "th": [2, 4]
    },
    "TSS": {
        "min": 0, 
        "max": 50, 
        "palette": ["#87CEEB", "#A98467", "#606C38"],
        "unit": "mg/L", 
        "th": [10, 30]
    },
    "CDOM": {
        "min": 0, 
        "max": 10, 
        "palette": ["#FFF9C4", "#FF9800", "#5D4037"],
        "unit": "m⁻¹", 
        "th": [1, 5]
    },
    "Cyanobacteria": {
        "min": 0, 
        "max": 5, 
        "palette": ["#FBC02D", "#E65100", "#136F63"],
        "unit": "cells/mL", 
        "th": [0.5, 2]
    },
}

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.json
        roi_coords = data.get('roi', [])
        selected_param = data.get('parameter', 'Turbidity')
        config = PARAM_CONFIG.get(selected_param, PARAM_CONFIG["Turbidity"])
        
        # Define Geometry from user ROI
        geometry = ee.Geometry.Polygon([roi_coords])

        # Load Sentinel-2 Collection
        image = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
                 .filterBounds(geometry)
                 .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
                 .sort('system:time_start', False).first())

        if not image:
            return jsonify({"status": "error", "message": "No satellite imagery found for this area."})

        # Create Water Mask using MNDWI
        mndwi = image.normalizedDifference(['B3', 'B11'])
        water_mask = mndwi.gt(0)

        # --- Parameter Calculations ---
        if selected_param == "Chlorophyll-a":
            layer = image.normalizedDifference(['B5', 'B4']).multiply(100).rename('val')

        elif selected_param == "Turbidity":
            layer = image.normalizedDifference(['B4', 'B3']).multiply(50).rename('val')

        elif selected_param == "TSS":
            layer = image.select('B4').divide(image.select('B3')).multiply(10).rename('val')

        elif selected_param == "Secchi Depth":
            layer = image.select('B2').divide(image.select('B3')).multiply(5).rename('val')

        elif selected_param == "CDOM":
            layer = image.select('B3').divide(image.select('B2')).multiply(8).rename('val')

        elif selected_param == "Cyanobacteria":
            layer = image.normalizedDifference(['B3', 'B4']).multiply(60).rename('val')

        else:
            layer = image.select('B4').multiply(0.0001).multiply(380).rename('val')

        # Apply mask and clip to ROI
        masked_layer = layer.updateMask(water_mask).clip(geometry)

        # Get Mean Value
        stats = masked_layer.reduceRegion(
            reducer=ee.Reducer.mean(), 
            geometry=geometry, 
            scale=10
        ).getInfo()
        
        mean_val = round(stats.get('val', 0), 2) if stats and stats.get('val') else 0

        # Area Calculation Logic
        area_img = ee.Image.pixelArea().updateMask(water_mask).clip(geometry)
        
        def get_area(mask):
            area_data = area_img.updateMask(mask).reduceRegion(
                reducer=ee.Reducer.sum(), 
                geometry=geometry, 
                scale=10,
                maxPixels=1e9
            ).getInfo()
            return area_data.get('area', 0)

        low_area = get_area(masked_layer.lt(config['th'][0]))
        mod_area = get_area(masked_layer.gte(config['th'][0]).And(masked_layer.lt(config['th'][1])))
        high_area = get_area(masked_layer.gte(config['th'][1]))

        # Generate Map ID for URL
        map_info = masked_layer.getMapId({
            'min': config['min'], 
            'max': config['max'], 
            'palette': config['palette']
        })

        return jsonify({
            "status": "success",
            "map_url": map_info['tile_fetcher'].url_format,
            "mean_val": mean_val,
            "area_low": f"{int(low_area):,} m²",
            "area_mod": f"{int(mod_area):,} m²",
            "area_high": f"{int(high_area):,} m²",
            "unit": config['unit']
        })

    except Exception as e:
        print(f"Final Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)})

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "message": "AquaSight Backend is running"
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
