from flask import Flask, request, jsonify
import joblib
import pandas as pd
import json
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore
import requests
import pprint
import hmac
import hashlib
from flask import abort
# -------------------------
# Initialize Flask + Firebase
# -------------------------
app = Flask(__name__)
cred = credentials.Certificate("new_firebase_key.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

try:
    test_doc = {"test": "connected", "timestamp": datetime.now().isoformat()}
    db.collection("diagnostic_test").add(test_doc)
    print("✅ Firestore connection confirmed with test document.")
except Exception as e:
    print("❌ Firestore diagnostic error:", e)

# -------------------------
# Load model + features
# -------------------------
model = joblib.load("eta_model.pkl")
with open("feature_names.json") as f:
    expected_cols = json.load(f)

# -------------------------
# Paystack Config
# -------------------------
PAYSTACK_SECRET_KEY = "sk_live_74f754782ab026977acbe9b41908bd4765f461bd"

@app.route("/initialize-payment", methods=["POST"])
def initialize_payment():
    data = request.get_json()
    email = data.get("email")
    amount_naira = data.get("Cost_of_the_Product")

    if not email or not amount_naira:
        return jsonify({"error": "Missing email or amount"}), 400

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "email": email,
        "amount": int(amount_naira) * 100,
        "currency": "GHS",
        "metadata": data,
        "callback_url": "http://localhost:8501"  # or a custom success page
    }

    try:
        response = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers)
        res_data = response.json()

        if response.status_code == 200 and res_data.get("status") is True:
            return jsonify({"authorization_url": res_data["data"]["authorization_url"]})
        else:
            return jsonify({"error": res_data.get("message", "Payment init failed")}), 500

    except Exception as e:
        return jsonify({"error": f"Payment initialization error: {str(e)}"}), 500

# -------------------------
# Predict ETA
# -------------------------
@app.route("/predict", methods=["POST"])
def predict_eta():
    try:
        data = request.get_json()
        input_df = pd.DataFrame([data])
        input_df = pd.get_dummies(input_df)
        input_df = input_df.reindex(columns=expected_cols, fill_value=0)

        prediction = model.predict(input_df)[0]
        label = "On Time" if prediction == 1 else "Late"

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "input": data,
            "prediction": label,
            "status": "completed"
        }
        db.collection("predictions").add(log_entry)

        return jsonify({"prediction": label})

    except Exception as e:
        app.logger.error(f"Prediction error: {str(e)}")
        db.collection("predictions").add({
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
            "status": "failed"
        })
        return jsonify({"error": f"Prediction failed: {str(e)}"}), 400

# -------------------------
# TrackingMore Configuration
# -------------------------
TRACKINGMORE_API_KEY = "qt9gxaou-f0me-f7av-ra0h-6mpu8pmx0l0j"

@app.route("/track", methods=["POST"])
def create_tracking():
    try:
        data = request.get_json()
        tracking_number = data["tracking_number"]
        carrier_code = data.get("carrier_code", "auto")

        headers = {
            "Content-Type": "application/json",
            "Tracking-Api-Key": TRACKINGMORE_API_KEY
        }

        payload = {
            "tracking_number": tracking_number,
            "carrier_code": carrier_code,
            "title": data.get("title", "ETA Prediction Shipment"),
            "customer_name": data.get("customer_name", "Topman User"),
            "customer_email": data.get("customer_email", "user@example.com")
        }

        response = requests.post("https://api.trackingmore.com/v3/trackings/create", headers=headers, json=payload)
        return jsonify(response.json())

    except Exception as e:
        return jsonify({"error": f"Tracking creation failed: {str(e)}"}), 400

@app.route("/track/status", methods=["GET"])
def get_tracking_status():
    try:
        tracking_number = request.args.get("tracking_number")
        carrier_code = request.args.get("carrier_code", "auto")

        if not tracking_number:
            return jsonify({"error": "tracking_number is required"}), 400

        headers = {
            "Content-Type": "application/json",
            "Tracking-Api-Key": TRACKINGMORE_API_KEY
        }

        url = f"https://api.trackingmore.com/v3/trackings/get?carrier_code={carrier_code}&tracking_number={tracking_number}"
        response = requests.get(url, headers=headers)
        data = response.json()

        print("📡 TrackingMore response:")
        pprint.pprint(data)

        meta_code = data.get("meta", {}).get("code") or data.get("code")
        if meta_code == 203:
            return jsonify({
                "error": " This feature requires a paid TrackingMore API plan. Please upgrade your account."
            }), 403

        if meta_code == 200:
            print(" Entered 200-code processing block.")

            shipment = data.get("data")
            if isinstance(shipment, list) and shipment:
                shipment = shipment[0]
            elif not isinstance(shipment, dict):
                print(" Unexpected tracking data format:", shipment)
                return jsonify({"error": "Unexpected tracking data format"}), 400

            print(" Normalized shipment:", shipment)

            delivery_status = shipment.get("delivery_status", "unknown")
            latest_event = shipment.get("latest_event", "")
            origin_info = shipment.get("origin_info", {})
            checkpoints = origin_info.get("trackinfo", [])

            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "tracking_number": tracking_number,
                "carrier_code": carrier_code,
                "delivery_status": delivery_status,
                "latest_event": latest_event,
                "checkpoints": checkpoints,
                "raw_data": shipment
            }

            try:
                db.collection("tracking_status_logs").add(log_entry)
                print(" Tracking status log successfully saved to Firestore.")
            except Exception as firestore_error:
                print(" Firestore logging error:", firestore_error)
        else:
            print(" Skipped logging — Unexpected meta/code value:", meta_code)

        return jsonify(data)

    except Exception as e:
        return jsonify({"error": f"Tracking status check failed: {str(e)}"}), 400
@app.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    print(" Incoming webhook hit at /paystack/webhook")

    # Step 1: Verify signature
    secret = PAYSTACK_SECRET_KEY.encode("utf-8")
    signature = request.headers.get("x-paystack-signature")
    raw_body = request.get_data()
    computed_signature = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()

    if signature != computed_signature:
        print(" Signature mismatch! Possible spoofed request.")
        abort(400)

    event = request.get_json()
    event_type = event.get("event")
    data = event.get("data", {})
    
    # Extract and parse metadata from custom_fields
    metadata = {}
    custom_fields = data.get("metadata", {}).get("custom_fields", [])
    for field in custom_fields:
        if field.get("variable_name") == "payload":
            try:
                metadata = json.loads(field.get("value", "{}"))
            except json.JSONDecodeError:
                print(" Failed to parse metadata JSON.")

    print("📦 Metadata received for prediction:", metadata)

    if event_type == "charge.success" and data.get("status") == "success":
        email = metadata.get("email", "unknown")

        # Log payment
        payment_entry = {
            "timestamp": datetime.now().isoformat(),
            "status": "success",
            "amount": data.get("amount") / 100,
            "email": email,
            "reference": data.get("reference"),
            "channel": data.get("channel"),
            "raw_response": data
        }
        db.collection("payments").add(payment_entry)

        # Step 2: Run prediction
        try:
            if model is None:
                raise ValueError(" Model is not loaded.")
            if expected_cols is None:
                raise ValueError(" expected_cols is not defined.")

            input_df = pd.DataFrame([metadata])
            for col in input_df.columns:
                try:
                    input_df[col] = pd.to_numeric(input_df[col])
                except (ValueError, TypeError):
                    pass

            input_df = pd.get_dummies(input_df)
            input_df = input_df.reindex(columns=expected_cols, fill_value=0)

            print(" Model loaded. Predicting ETA...")
            prediction = model.predict(input_df)[0]
            label = "On Time" if prediction == 1 else "Late"

            print(f"📊 Prediction result: {label}")
            prediction_log = {
                "timestamp": datetime.now().isoformat(),
                "input": metadata,
                "prediction": label,
                "status": "completed",
                "email": email,
                "payment_reference": data.get("reference")
            }
            db.collection("predictions").add(prediction_log)
            print(" Prediction logged to Firestore.")

        except Exception as e:
            error_message = str(e)
            print(f" Prediction after payment failed: {error_message}")
            db.collection("predictions").add({
                "timestamp": datetime.now().isoformat(),
                "error": error_message,
                "email": email,
                "status": "failed"
            })

    return jsonify({"status": "webhook received"}), 200


# -------------------------
# Run Server
# -------------------------
if __name__ == "__main__":
    app.run(debug=True)
