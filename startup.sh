#!/bin/bash

# Update system and install Tesseract OCR and dependencies
echo "Installing Tesseract OCR..."
apt-get update
apt-get install -y tesseract-ocr libtesseract-dev poppler-utils

# Install Python dependencies using requirements file
echo "Installing Python dependencies..."
pip install --no-cache-dir -r requirements.txt

# Check if Tesseract is installed successfully
echo "Checking Tesseract version..."
which tesseract

# Check if Poppler is installed
echo "Checking Poppler (pdftoppm)..."
which pdftoppm

# Start the FastAPI app with Uvicorn
echo "Starting FastAPI app..."
uvicorn app:app --host 0.0.0.0 --port 8000