from pydantic import BaseModel, Field
from typing import Optional

class CustomerCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)

class SiteCreate(BaseModel):
    customer_id: str
    name: str = Field(min_length=2, max_length=120)

class DeviceCreate(BaseModel):
    customer_id: str
    site_id: Optional[str] = None
    name: str = Field(min_length=2, max_length=120)
    vendor: str = "MikroTik"
    model: Optional[str] = None

class Heartbeat(BaseModel):
    routeros_version: Optional[str] = None
    wan_ip: Optional[str] = None
    uptime: Optional[str] = None
    cpu_load: Optional[str] = None
    free_memory: Optional[str] = None
