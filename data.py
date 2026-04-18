import csv
from datetime import datetime
from collections import defaultdict


def compute_hourly_averages(filename="lund_2025_param120.csv", invalid_value=-999):
    """
    Compute average W/m² for each hour of the day (0-23),
    skipping invalid data points (-999).
    
    Returns:
        dict: {hour: average_wm2} for hours 0-23
    """
    hourly_sums = defaultdict(float)
    hourly_counts = defaultdict(int)
    skipped = 0
    
    with open(filename, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                value = float(row["value"])
            except (ValueError, TypeError):
                skipped += 1
                continue
            
            # Skip invalid data
            if value == invalid_value or value < 0:
                skipped += 1
                continue
            
            # Parse the datetime and extract the hour
            dt = datetime.fromisoformat(row["date_time"].replace("Z", "+00:00"))
            hour = dt.hour
            
            hourly_sums[hour] += value
            hourly_counts[hour] += 1
    
    # Compute averages
    hourly_averages = {}
    for hour in range(24):
        if hourly_counts[hour] > 0:
            hourly_averages[hour] = hourly_sums[hour] / hourly_counts[hour]
        else:
            hourly_averages[hour] = 0.0
    
    print(f"Skipped {skipped} invalid/missing data points")
    return hourly_averages


def compute_yearly_average(filename="lund_2025_param120.csv", invalid_value=-999):
    """
    Compute the overall average W/m² for the entire year,
    skipping invalid data points (-999).
    
    Returns:
        float: average W/m² across all valid data points
    """
    total = 0.0
    count = 0
    skipped = 0
    
    with open(filename, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                value = float(row["value"])
            except (ValueError, TypeError):
                skipped += 1
                continue
            
            if value == invalid_value or value < 0:
                skipped += 1
                continue
            
            total += value
            count += 1
    
    if count == 0:
        print("No valid data points found!")
        return 0.0
    
    average = total / count
    return average, count, skipped

def load_wattage_data(filename="lund_2025_param120.csv", invalid_value=-999):
    """
    Load solar irradiance data into a dict keyed by datetime (UTC).
    Skips invalid (-999) values.
    
    Returns:
        dict: {datetime_utc: wattage_wm2}
    """
    data = {}
    skipped = 0
    
    with open(filename, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                value = float(row["value"])
            except (ValueError, TypeError):
                skipped += 1
                continue
            
            if value == invalid_value or value < 0:
                skipped += 1
                continue
            
            # Parse and normalize to UTC
            dt = datetime.fromisoformat(row["date_time"].replace("Z", "+00:00"))
            data[dt] = value
    
    print(f"Loaded {len(data)} wattage points (skipped {skipped} invalid)")
    return data


def load_price_data(filename="lund_2025_elpris_SE4.csv"):
    """
    Load electricity price data into a dict keyed by datetime (UTC).
    Converts from Swedish local time to UTC for matching.
    
    Returns:
        dict: {datetime_utc: price_sek_per_kwh}
    """
    data = {}
    
    with open(filename, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Price timestamps are in Swedish local time (+01:00 or +02:00)
            dt = datetime.fromisoformat(row["date_time"])
            # Convert to UTC for matching with wattage data
            dt_utc = dt.astimezone(tz=datetime.now().astimezone().tzinfo).utctimetuple()
            # Better: use timezone-aware UTC conversion
            from datetime import timezone
            dt_utc = dt.astimezone(timezone.utc)
            
            price = float(row["price_sek_per_kwh"])
            data[dt_utc] = price
    
    print(f"Loaded {len(data)} price points")
    return data


def combine_data(wattage_file="lund_2025_param120.csv",
                price_file="lund_2025_elpris_SE4.csv"):
    """
    Combine wattage and price data by matching timestamps.
    
    Returns:
        list of tuples: [(datetime_utc, wattage_wm2, price_sek_per_kwh), ...]
    """
    wattage_data = load_wattage_data(wattage_file)
    price_data = load_price_data(price_file)
    
    # Find common timestamps
    common_times = sorted(set(wattage_data.keys()) & set(price_data.keys()))
    
    combined = [
        (dt, wattage_data[dt], price_data[dt])
        for dt in common_times
    ]
    
    # Stats about matching
    only_wattage = set(wattage_data.keys()) - set(price_data.keys())
    only_price = set(price_data.keys()) - set(wattage_data.keys())
    
    print(f"\n{'=' * 50}")
    print(f"Matching results:")
    print(f"  Combined datapoints: {len(combined)}")
    print(f"  Only in wattage data: {len(only_wattage)}")
    print(f"  Only in price data: {len(only_price)}")
    print(f"{'=' * 50}")
    
    return combined


def save_combined_csv(combined, filename="lund_2025_combined.csv"):
    """Save the combined data as a CSV for easy inspection."""
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date_time_utc", "wattage_wm2", "price_sek_per_kwh"])
        for dt, watt, price in combined:
            writer.writerow([dt.isoformat(), watt, price])
    print(f"Saved combined CSV: {filename}")


def main():
    combined = combine_data(
        wattage_file="lund_2025_param120.csv",
        price_file="lund_2025_elpris_SE4.csv"
    )
    
    # Show a sample
    print(f"\nFirst 5 datapoints:")
    for dt, watt, price in combined[:5]:
        print(f"  {dt.isoformat()}  →  {watt:7.2f} W/m²  |  {price:.4f} SEK/kWh")
    
    print(f"\nLast 5 datapoints:")
    for dt, watt, price in combined[-5:]:
        print(f"  {dt.isoformat()}  →  {watt:7.2f} W/m²  |  {price:.4f} SEK/kWh")
    
    # Save for later use
    save_combined_csv(combined)
    
    # Quick summary statistics
    if combined:
        watts = [c[1] for c in combined]
        prices = [c[2] for c in combined]
        
        print(f"\n{'=' * 50}")
        print(f"Summary:")
        print(f"  Mean W/m²:    {sum(watts) / len(watts):.2f}")
        print(f"  Mean SEK/kWh: {sum(prices) / len(prices):.4f}")
        print(f"{'=' * 50}")
    
    return combined