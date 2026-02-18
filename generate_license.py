"""
License Key Generator
Utility to generate license keys for AircraftTracker
"""

import hashlib
import secrets
import sys
from datetime import datetime, timedelta


def generate_license_key(email: str, tier: str = 'single') -> str:
    """Generate a unique license key"""
    seed = f"{email}{datetime.now().isoformat()}{secrets.token_hex(16)}"
    hash_obj = hashlib.sha256(seed.encode())
    hash_hex = hash_obj.hexdigest()
    
    # Format as KDTO-XXXX-XXXX-XXXX-XXXX
    key_parts = [
        hash_hex[0:4].upper(),
        hash_hex[4:8].upper(),
        hash_hex[8:12].upper(),
        hash_hex[12:16].upper()
    ]
    
    license_key = f"KDTO-{'-'.join(key_parts)}"
    return license_key


def print_sql_insert(license_key: str, email: str, tier: str, activations_max: int, expires_days: int = None):
    """Generate SQL INSERT statement"""
    
    tier_map = {
        'single': 1,
        'school': 5,
        'enterprise': -1  # unlimited
    }
    
    activations = activations_max if activations_max else tier_map.get(tier, 1)
    
    expires_sql = "NULL"
    if expires_days:
        expires_date = datetime.now() + timedelta(days=expires_days)
        expires_sql = f"'{expires_date.strftime('%Y-%m-%d')}'"
    
    sql = f"""
INSERT INTO licenses (id, license_key, tier, activations_used, activations_max, expires_at, status, created_at)
VALUES (
    gen_random_uuid(),
    '{license_key}',
    '{tier}',
    0,
    {activations},
    {expires_sql},
    'active',
    NOW()
);
"""
    print(sql)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python generate_license.py <email> <tier> [activations_max] [expires_days]")
        print("\nTiers: single, school, enterprise")
        print("activations_max: Number of allowed activations (default: 1 for single, 5 for school, -1 for enterprise)")
        print("expires_days: Days until expiration (optional, default: lifetime)")
        print("\nExample: python generate_license.py john@flightschool.com school")
        sys.exit(1)
    
    email = sys.argv[1]
    tier = sys.argv[2]
    activations_max = int(sys.argv[3]) if len(sys.argv) > 3 else None
    expires_days = int(sys.argv[4]) if len(sys.argv) > 4 else None
    
    if tier not in ['single', 'school', 'enterprise']:
        print("Error: tier must be 'single', 'school', or 'enterprise'")
        sys.exit(1)
    
    # Generate key
    license_key = generate_license_key(email, tier)
    
    print("=" * 70)
    print(f"LICENSE KEY GENERATED")
    print("=" * 70)
    print(f"Email: {email}")
    print(f"Tier: {tier}")
    print(f"License Key: {license_key}")
    print("=" * 70)
    print("\nSQL INSERT Statement:")
    print("-" * 70)
    
    print_sql_insert(license_key, email, tier, activations_max, expires_days)
    
    print("=" * 70)
    print("\nCopy the SQL statement above and run it in your PostgreSQL database.")
    print("Then provide the license key to the customer.")
    print("=" * 70)
