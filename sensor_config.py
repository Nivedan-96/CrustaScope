# Import required libraries for sensor reading, I2C communication, time handling, and MongoDB storage
import os
import time
import json
from datetime import datetime

# Hardware communication libraries for Raspberry Pi sensors
import board
import busio

# MongoDB client to store sensor readings in the cloud
import pymongo

# Library to read DS18B20 temperature sensor using 1-Wire protocol
try:
    from w1thermsensor import W1ThermSensor
except Exception as e:
    W1ThermSensor = None

# Libraries for ADS1115 ADC used to read analog sensors (pH, turbidity, TDS)
from adafruit_ads1x15.ads1115 import ADS1115
from adafruit_ads1x15.analog_in import AnalogIn


# Configuration parameters for sensor data collection and storage
SENSOR_DB_INTERVAL_SECONDS = float(os.getenv("SENSOR_DB_INTERVAL_SECONDS", "300"))  # Interval (seconds) to save sensor readings to MongoDB

LOOP_INTERVAL_SECONDS = float(os.getenv("SENSOR_LOOP_INTERVAL_SECONDS", "2"))  # Delay between each sensor read cycle

LATEST_JSON = "latest_sensor.json"  # Local file used to store the latest sensor readings for the dashboard


# MongoDB connection variables for storing water quality measurements
MONGO_URI = os.getenv("MONGODB_URI")
client = None
db = None
sensor_collection = None



# Establish MongoDB connection for storing periodic sensor readings
if MONGO_URI:
    try:
        client = pymongo.MongoClient(MONGO_URI)  # Create MongoDB client using connection URI
        db = client["crustascope"]               # Select the CrustaScope database
        sensor_collection = db["sensor_results"] # Collection used to store water quality logs
        print("[INFO] Connected to MongoDB for sensor logging.")
    except Exception as e:
        print("[WARN] MongoDB connection failed:", e)
else:
    print("[WARN] MONGODB_URI not set. Sensor data will not be stored to DB.")


# Initialize DS18B20 temperature sensor connected via Raspberry Pi 1-Wire interface
print("[INFO] Initializing DS18B20 temperature sensor...")
try:
    if W1ThermSensor:
        temp_sensor = W1ThermSensor()  # Attempt to detect and initialize the DS18B20 sensor
        print("[INFO] DS18B20 detected.")
    else:
        raise Exception("W1ThermSensor module failed to load.")
except Exception as e:
    print("[WARN] DS18B20 not available:", e)  # If sensor is not connected or driver not loaded
    temp_sensor = None  # Disable temperature readings if sensor initialization fails
    
    

# Initialize I2C communication and ADS1115 ADC to read analog sensors
print("[INFO] Initializing I2C + ADS1115...")

try:
    # Create I2C bus using Raspberry Pi SCL and SDA pins
    i2c = busio.I2C(board.SCL, board.SDA)

    # Initialize ADS1115 analog-to-digital converter
    ads = ADS1115(i2c)

    # Set ADC gain (defines voltage measurement range)
    ads.gain = 1

    # Set sampling rate for faster sensor readings
    ads.data_rate = 860

    # Define ADC channels used for different sensors
    ch_tds  = AnalogIn(ads, 0)   # Channel A0 → TDS sensor
    ch_ph   = AnalogIn(ads, 1)   # Channel A1 → pH sensor
    ch_turb = AnalogIn(ads, 3)   # Channel A3 → Turbidity sensor
except Exception as e:
    print(f"[WARN] I2C/ADS1115 initialization failed: {e}")
    ads = None
    ch_tds = None
    ch_ph = None
    ch_turb = None


# Convert pH sensor voltage reading into an approximate pH value using calibration formula
def convert_ph(v):
    if v is None:
        return None
    ph = 7 + ((2.5 - v) / 0.18)   # Convert voltage to pH based on calibration
    return round(ph, 2)




# Calibration constants for turbidity conversion from voltage to NTU
V_CLEAR = 3.64     # Voltage measured in clear tap water (baseline ~0 NTU)
V_LIGHT_DIRTY = 3.20  # Reference voltage assumed for slightly muddy water (~10 NTU)
SCALE_FACTOR = 22.7   # Scaling factor used to convert voltage difference into NTU

# Convert turbidity sensor voltage into NTU using calibrated voltage references
def convert_turbidity(v):
    if v is None:
        return None

    # If voltage above clear-water voltage, treat as 0 NTU
    if v >= V_CLEAR:
        return 0.0

    ntu = (V_CLEAR - v) * SCALE_FACTOR  # Estimate turbidity based on voltage drop from clear-water reference

    # Prevent negative values
    if ntu < 0:
        ntu = 0

    return round(ntu, 2)



# Calibration factor used to adjust raw TDS calculations to match real-world measurements
TDS_CALIBRATION_FACTOR = 0.63  

# Convert TDS sensor voltage into Total Dissolved Solids (PPM) with temperature compensation
def convert_tds(v, temp_c):
    if v is None or temp_c is None:
        return None

    # Ignore very small voltages (sensor noise / no water)
    if v < 0.01:
        return 0.0

    # Convert sensor voltage to electrical conductivity using sensor polynomial
    ec = (133.42 * v**3 - 255.86 * v**2 + 857.39 * v)

    # Apply temperature compensation to normalize EC to 25°C
    ec25 = ec / (1 + 0.02 * (temp_c - 25.0))

    # Convert electrical conductivity to TDS (PPM)
    tds = ec25 / 2.0

    # Apply calibration factor to match real-world water readings
    tds = tds * TDS_CALIBRATION_FACTOR

    # Prevent negative values
    if tds < 0:
        tds = 0.0

    return round(tds, 2)


    

# Track last time sensor data was saved to MongoDB to enforce logging interval
last_db_save = 0.0

# Print startup message showing sensor loop interval and database logging interval
print(f"[INFO] Sensor reader loop started (every {LOOP_INTERVAL_SECONDS}s, "
      f"DB logging every {SENSOR_DB_INTERVAL_SECONDS}s).")

# Continuous sensor acquisition loop that reads hardware sensors, updates live JSON for the dashboard,
# and periodically stores water quality data into MongoDB for historical logging
while True:
    try:
        # Read temperature from DS18B20 sensor if available
        if temp_sensor:
            try:
                temp_c = round(temp_sensor.get_temperature(), 2)
            except Exception as e:
                print("[WARN] Temp read failed:", e)
                temp_c = None
        else:
            temp_c = None

        # Read raw voltage values from ADS1115 analog channels
        if ads:
            v_tds  = ch_tds.voltage
            v_ph   = ch_ph.voltage
            v_turb = ch_turb.voltage
        else:
            v_tds = 0.0
            v_ph = 0.0
            v_turb = 0.0

        # Convert raw voltages into calibrated water-quality values
        ph_val   = convert_ph(v_ph) if ads else None
        turb_val = convert_turbidity(v_turb) if ads else None
        tds_val  = convert_tds(v_tds, temp_c) if ads else None

        # Generate timestamp for the sensor reading
        now_iso = datetime.now().isoformat()

        # Build structured sensor document containing processed values and raw voltages
        sensor_doc = {
            "timestamp": now_iso,
            "temperature_c": temp_c,
            "ph": ph_val,
            "turbidity": turb_val,
            "tds": tds_val,
            "raw_voltages": {
                "tds_v": v_tds,
                "ph_v": v_ph,
                "turb_v": v_turb,
            },
        }

        # Update local JSON file used by the backend API to provide live sensor data
        try:
            with open(LATEST_JSON, "w") as f:
                json.dump(sensor_doc, f, indent=2)
        except Exception as e:
            print("[WARN] Could not write latest_sensor.json:", e)

        # Print real-time sensor readings in the terminal for monitoring/debugging
        print(
            "[LIVE]",
            f"T={temp_c}°C",
            f"pH={ph_val} (v={v_ph:.4f}V)",
            f"NTU={turb_val} (v={v_turb:.4f}V)",
            f"TDS={tds_val}ppm (v={v_tds:.4f}V)",
        )

        # Periodically store sensor readings in MongoDB for historical analysis
        now_ts = time.time()
        if sensor_collection is not None and (now_ts - last_db_save) >= SENSOR_DB_INTERVAL_SECONDS:
            try:
                sensor_collection.insert_one(sensor_doc)
                last_db_save = now_ts
                print("[INFO] Sensor reading saved to MongoDB.")
            except Exception as e:
                print("[WARN] MongoDB insert failed:", e)

    # Gracefully stop loop when user presses CTRL+C
    except KeyboardInterrupt:
        print("[INFO] Sensor reader stopped by user.")
        break

    # Catch unexpected errors to prevent sensor loop from crashing
    except Exception as e:
        print("[ERROR] Unexpected sensor loop error:", e)

    # Wait before next sensor reading cycle
    time.sleep(LOOP_INTERVAL_SECONDS)
