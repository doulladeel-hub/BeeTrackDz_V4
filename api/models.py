"""
models.py — Pydantic models shared across the application.
"""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class EnvironmentData(BaseModel):
    temperature: float = 0.0
    humidity: float = 0.0


class GPSData(BaseModel):
    latitude: float = 0.0
    longitude: float = 0.0


class HiveData(BaseModel):
    weight: float = 0.0


class SystemData(BaseModel):
    battery: int = 0
    sound: int = 0
    swarm: bool = False


class SensorDataIn(BaseModel):
    environment: EnvironmentData = Field(default_factory=EnvironmentData)
    gps: GPSData = Field(default_factory=GPSData)
    hive: HiveData = Field(default_factory=HiveData)
    system: SystemData = Field(default_factory=SystemData)
    hive_id: Optional[str] = None


class SensorDataOut(BaseModel):
    hive_id: str
    timestamp: datetime
    environment: EnvironmentData
    gps: GPSData
    hive: HiveData
    system: SystemData