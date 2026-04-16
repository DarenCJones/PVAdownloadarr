# Kentucky Beacon Owner Scraper

A Python script that reads parcel IDs from a CSV file, retrieves
property owner information from Beacon/Schneider county websites, and
saves the results to a new CSV.

------------------------------------------------------------------------

## Overview

This tool automates the process of gathering owner mailing information
for parcels across multiple Kentucky counties.

For each parcel, the script:

1.  Reads `county` and `parcel_id` from an input CSV file\
2.  Maps the county to the correct Beacon `AppID`\
3.  Builds the parcel URL\
4.  Opens the page using Playwright\
5.  Clicks through agreement pages if needed\
6.  Extracts:
    -   Owner name\
    -   Address line 1\
    -   City / State / ZIP\
7.  Writes results to an output CSV

------------------------------------------------------------------------

## Features

-   Multi-county support (built-in AppID lookup)
-   CSV input and output
-   Resume capability (skips already processed records)
-   Incremental saving (safe to stop anytime)
-   Handles "Primary Owner" edge case
-   Stops early if too many failed records occur
-   Uses a fresh browser page per record for reliability

------------------------------------------------------------------------

## File Structure
```text
PVA/
├── ParcelID.csv        # Input file
├── owner_output.csv    # Output file (generated)
└── PVAdownloadarr.py   # Main script
```
------------------------------------------------------------------------

## Installation & Setup

### 1. Install Python

Requires Python 3.11+

Check: python --version

Download if needed: https://www.python.org/downloads/

------------------------------------------------------------------------

### 2. Install Dependencies

pip install playwright

------------------------------------------------------------------------

### 3. Install Browsers

python -m playwright install

------------------------------------------------------------------------

## Input File

ParcelID.csv format:

county,parcel_id Franklin,085-10-13-015.00 Floyd,045-30-05-001.00

------------------------------------------------------------------------

## Run Script

python scraper.py

------------------------------------------------------------------------

## Output

owner_output.csv will be created.

------------------------------------------------------------------------

## Resume Behavior

-   Script skips already processed records
-   Safe to stop and restart

------------------------------------------------------------------------

## Disclaimer

Relies on Beacon website structure. Changes may require updates.
