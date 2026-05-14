#!/usr/bin/env python3
"""Standalone entry point for the data collector.

Run independently of the web server:
    python run_collector.py
Or via Docker as a separate service.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.collector import run_collector

if __name__ == "__main__":
    asyncio.run(run_collector())
