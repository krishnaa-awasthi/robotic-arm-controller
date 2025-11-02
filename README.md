# TechExpo 2025 – Robotic Bomb Diffuser Control System

### 🎯 Overview
A simulated robotic bomb diffuser built for TechExpo 2025.  
This project demonstrates a safe remote control system for robotic operations with real-time telemetry, emergency stop, and mission logging.

### 🧠 Features
- FastAPI + WebSocket backend
- Real-time telemetry and command exchange
- Emergency stop and safety checks
- Mission logging to persistent files
- Frontend control dashboard
- Secure authentication token system

### 🧩 Tech Stack
- **Backend:** Python, FastAPI, Uvicorn, asyncio  
- **Frontend:** HTML, CSS, JavaScript (WebSocket)  

### ⚙️ Run Locally
```bash
# Run backend
cd backend
pip install -r requirements.txt
python app.py

# Run frontend
cd frontend
python -m http.server 8000
