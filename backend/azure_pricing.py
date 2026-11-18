import json
import math
import os
import re

import requests

AZURE_PRICING_API = "https://prices.azure.com/api/retail/prices"

REGION = "eastus"

MONTHLY_HOURS = 744

# Default OS disk attached to every VM (GiB). Override per VM with "OS_Disk_GB"
# in the BOM, or set to 0 to exclude the OS disk.
DEFAULT_OS_DISK_GB = 128

# Premium SSD tiers (name, size in GiB). These are product sizes, not prices.
DISK_TIERS = [
    ("P4", 32), ("P6", 64), ("P10", 128), ("P15", 256), ("P20", 512),
    ("P30", 1024), ("P40", 2048), ("P50", 4096), ("P60", 8192),
    ("P70", 16384), ("P80", 32767),
]

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

GEMINI_SYSTEM_PROMPT = f"""
You are an Azure cost estimation engine. You receive a BOM (bill of materials)
as JSON, written for Oracle Cloud (OCI). Price it on Azure.

RULES
1. Never invent or recall prices. Every price MUST come from the
   search_azure_prices tool (Azure Retail Pricing API). Call it as many times
   as needed. If no price is found for an item, set its monthly_cost to 0 and
   add a message to "warnings".
2. Region is '{REGION}'. Monthly hours = {MONTHLY_HOURS}. Currency is USD.
3. 1 OCI OCPU = 2 Azure vCPUs. Convert before picking a VM size.
4. Compute: pick the Azure VM arm_sku from vCPUs (>=32 Standard_D32s_v5,
   >=16 Standard_D16s_v5, >=8 Standard_D8s_v5, else Standard_D4s_v5).
   service_name 'Virtual Machines'.
   VM cost = hourly price x {MONTHLY_HOURS} x Count.
   Each VM ALSO has an OS disk of {DEFAULT_OS_DISK_GB} GiB (use the VM's
   "OS_Disk_GB" if present; if it is 0, skip the OS disk). Price it exactly like
   Block storage (rule 5) and multiply by the VM Count.
   OS disk cost = OS disk count x price per disk per month x VM Count.
   The VM's monthly_cost = VM cost + OS disk cost. Show both parts in
   "calculation" and mention the OS disk in "explanation".
5. Storage / Block: Premium SSD Managed Disks, service_name 'Storage',
   product 'Premium SSD Managed Disks', sku '<tier> LRS' (meter contains
   '<tier> LRS Disk'). Pick the smallest tier from this list (name, GiB) that
   fits Storage_GB: {DISK_TIERS}. Disk count = ceil(Storage_GB / tier size).
   Cost = disk count x price per disk per month.
6. Storage / Object: product 'General Block Blob v2', sku 'Hot LRS'.
   Storage meter contains 'Data Stored'; operations meter contains
   'Write Operations'. Operations are priced per unitOfMeasure (e.g. 10K),
   so divide by that quantity to get price per request.
7. Storage / File: product 'Premium Files', sku 'Premium LRS', meter
   contains 'Provisioned'. Cost = GB x price.
8. Load Balancer: service_name 'Load Balancer', sku 'Standard'. Base price is
   the 'Included LB Rules' hourly meter x {MONTHLY_HOURS}; data charge is the
   'Data Processed' meter x (Bandwidth_Mbps / 1024 x 1024) GB.
9. ExpressRoute (FastConnect equivalent): service_name 'ExpressRoute', meter
   contains 'Port'. Cost = Port_Hours x hourly price.
10. API Management: service_name 'API Management', sku 'Consumption', meter
    contains 'Calls'. Convert the price to per-million-calls using unitOfMeasure.
11. Skip any item whose productName/skuName/meterName contains spot,
    low priority or windows. Prefer the lowest tierMinimumUnits.
12. Do the arithmetic carefully and show it in "calculation".

OUTPUT: reply with ONLY a JSON object (no markdown, no commentary):
{{
  "warnings": [string],
  "breakdown": [
    {{
      "service": string,
      "sku": string (optional),
      "inputs": object,
      "pricing": object (include "Price Source": "Azure Retail Pricing API"),
      "formula": string,
      "calculation": string,
      "monthly_cost": number,
      "explanation": string
    }}
  ]
}}
"""


class AzurePricingCalculator:

    def __init__(self):
        self.total_cost = 0
        self.breakdown = []
        self.warnings = []
        self._cache = {}

    ###############################################################
    # Generic Azure Retail Pricing API
    ###############################################################

    def _fetch(self, filter_query):
        """Call the API (following pagination) and cache by filter."""

        if filter_query in self._cache:
            return self._cache[filter_query]

        items = []
        url = AZURE_PRICING_API
        params = {"$filter": filter_query}

        while url:
            response = requests.get(url, params=params, timeout=30)

            if response.status_code != 200:
                break

            data = response.json()
            items.extend(data.get("Items", []))

            url = data.get("NextPageLink")
            params = None  # NextPageLink already contains the filter

        self._cache[filter_query] = items
        return items

    def get_price_item(
        self,
        service_name,
        arm_sku=None,
        product=None,
        sku=None,
        meter_contains=None,
        exclude=("spot", "low priority", "windows"),
    ):
        """Return the best matching price item (dict) or None."""

        filter_query = (
            f"serviceName eq '{service_name}'"
            f" and armRegionName eq '{REGION}'"
            f" and priceType eq 'Consumption'"
        )

        if arm_sku:
            filter_query += f" and armSkuName eq '{arm_sku}'"
        if product:
            filter_query += f" and productName eq '{product}'"
        if sku:
            filter_query += f" and skuName eq '{sku}'"

        candidates = []

        for item in self._fetch(filter_query):

            text = " ".join([
                item.get("productName", ""),
                item.get("skuName", ""),
                item.get("meterName", ""),
            ]).lower()

            if any(word in text for word in exclude):
                continue

            if meter_contains and meter_contains.lower() not in item.get("meterName", "").lower():
                continue

            if item.get("unitPrice", 0) <= 0:
                continue

            candidates.append(item)

        if not candidates:
            return None

        # Prefer the lowest pricing tier
        candidates.sort(key=lambda i: i.get("tierMinimumUnits", 0))
        return candidates[0]

    def get_price(self, *args, **kwargs):
        item = self.get_price_item(*args, **kwargs)
        return item["unitPrice"] if item else None

    @staticmethod
    def unit_divisor(unit_of_measure):
        """'10K' -> 10000, '1M' -> 1000000, '1 Hour' -> 1"""

        match = re.match(r"\s*([\d,\.]+)\s*([KkMm]?)", unit_of_measure or "")

        if not match:
            return 1

        number = float(match.group(1).replace(",", ""))
        suffix = match.group(2).upper()

        return number * {"": 1, "K": 1_000, "M": 1_000_000}[suffix]

    def search_prices(self, service_name, contains=""):
        """Debug helper: print meters so you can verify the filters below."""

        filter_query = (
            f"serviceName eq '{service_name}'"
            f" and armRegionName eq '{REGION}'"
            f" and priceType eq 'Consumption'"
        )

        for item in self._fetch(filter_query):
            line = " | ".join([
                item.get("productName", ""),
                item.get("skuName", ""),
                item.get("meterName", ""),
                item.get("unitOfMeasure", ""),
                str(item.get("unitPrice")),
            ])
            if contains.lower() in line.lower():
                print(line)

    def _record_missing(self, service, detail):
        """No price found: record it instead of inventing a number."""

        message = f"{service}: no price found in Azure Retail Pricing API ({detail})"
        self.warnings.append(message)

        self.breakdown.append({
            "service": service,
            "monthly_cost": 0,
            "explanation": message
        })

    ###############################################################
    # Managed Disk helper (shared by VM OS disks and Block storage)
    ###############################################################

    def _managed_disk(self, gb):
        """Pick the smallest Premium SSD tier that fits `gb`.

        Returns (tier, tier_gb, disk_count, price_per_disk). price_per_disk is
        None when the API has no price for that tier.
        """

        tier, tier_gb = next(
            ((n, s) for n, s in DISK_TIERS if s >= gb),
            DISK_TIERS[-1]
        )

        disk_count = math.ceil(gb / tier_gb)

        price = self.get_price(
            "Storage",
            product="Premium SSD Managed Disks",
            sku=f"{tier} LRS",
            meter_contains=f"{tier} LRS Disk"
        )

        return tier, tier_gb, disk_count, price

    ###############################################################
    # Compute (VM + OS disk)
    ###############################################################

    def calculate_compute(self, vm):

        ocpu = vm["OCPU"]
        memory = vm["Memory_GB"]
        count = vm["Count"]
        os_disk_gb = vm.get("OS_Disk_GB", DEFAULT_OS_DISK_GB)

        vcpus = ocpu * 2  # 1 OCI OCPU = 2 Azure vCPUs

        if vcpus >= 32:
            sku = "Standard_D32s_v5"
        elif vcpus >= 16:
            sku = "Standard_D16s_v5"
        elif vcpus >= 8:
            sku = "Standard_D8s_v5"
        else:
            sku = "Standard_D4s_v5"

        price_per_hour = self.get_price(
            "Virtual Machines",
            arm_sku=sku
        )

        if price_per_hour is None:
            self._record_missing("Azure Virtual Machine", sku)
            return

        monthly_hours = MONTHLY_HOURS

        vm_cost = price_per_hour * monthly_hours * count

        # ----- OS disk (one per VM) -----
        os_disk_cost = 0
        os_disk_tier = None
        os_disk_price = None
        os_disk_count = 0

        if os_disk_gb > 0:
            os_disk_tier, _, os_disk_count, os_disk_price = self._managed_disk(os_disk_gb)

            if os_disk_price is None:
                self.warnings.append(
                    f"Azure Virtual Machine: no price found for OS disk "
                    f"({os_disk_tier} LRS); OS disk cost excluded"
                )
            else:
                os_disk_cost = os_disk_count * os_disk_price * count

        cost = vm_cost + os_disk_cost

        self.total_cost += cost

        pricing = {
            "Price Source": "Azure Retail Pricing API",
            "Hourly Price": round(price_per_hour, 4),
            "Monthly Hours": monthly_hours
        }

        if os_disk_price is not None:
            pricing["OS Disk Price / Month"] = os_disk_price

        if os_disk_price is not None:
            formula = (
                "Monthly Cost = (Hourly Price × Monthly Hours × VM Count) "
                "+ (OS Disk Count × OS Disk Price × VM Count)"
            )
            calculation = (
                f"({price_per_hour:.4f} × {monthly_hours} × {count}) "
                f"+ ({os_disk_count} × {os_disk_price} × {count}) "
                f"= {vm_cost:.2f} + {os_disk_cost:.2f} = {cost:.2f}"
            )
            disk_note = (
                f" Each VM also includes a {os_disk_gb} GB OS disk "
                f"({os_disk_count} × {os_disk_tier} Premium SSD), billed per disk per month."
            )
        else:
            formula = "Monthly Cost = Hourly Price × Monthly Hours × VM Count"
            calculation = f"{price_per_hour:.4f} × {monthly_hours} × {count} = {cost:.2f}"
            disk_note = " OS disk is not included."

        self.breakdown.append({

            "service": "Azure Virtual Machine",

            "sku": sku,

            "inputs": {
                "OCPU": ocpu,
                "vCPU": vcpus,
                "Memory (GB)": memory,
                "VM Count": count,
                "OS Disk (GB)": os_disk_gb,
                "OS Disk Tier": os_disk_tier
            },

            "pricing": pricing,

            "formula": formula,

            "calculation": calculation,

            "monthly_cost": round(cost, 2),

            "cost_components": {
                "vm_compute": round(vm_cost, 2),
                "os_disk": round(os_disk_cost, 2)
            },

            "explanation": (
                f"Selected Azure SKU '{sku}' based on {ocpu} OCPU "
                f"({vcpus} vCPUs, since 1 OCPU = 2 vCPUs). "
                f"Azure charges Virtual Machines on an hourly basis. "
                f"The hourly price is multiplied by {monthly_hours} hours "
                f"and then multiplied by the number of VMs."
                f"{disk_note}"
            )
        })

    ###############################################################
    # Managed Disk (Block storage from the BOM)
    ###############################################################

    def calculate_block_storage(self, storage):

        gb = storage["Storage_GB"]

        # Managed disks are priced per disk tier per month, not per GB
        tier, tier_gb, disk_count, price = self._managed_disk(gb)

        if price is None:
            self._record_missing("Managed Disk", f"{tier} LRS")
            return

        cost = disk_count * price

        self.total_cost += cost

        self.breakdown.append({

            "service": "Managed Disk",

            "inputs": {
                "Disk Size (GB)": gb,
                "Disk Tier": tier,
                "Disk Count": disk_count
            },

            "pricing": {
                "Price per Disk / Month": price,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "Monthly Cost = Disk Count × Price per Disk",

            "calculation":
                f"{disk_count} × {price} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                f"Azure Managed Disks are billed per provisioned disk tier. "
                f"{gb} GB maps to {disk_count} × {tier} ({tier_gb} GiB)."
        })

    ###############################################################
    # Blob Storage
    ###############################################################

    def calculate_object_storage(self, obj):

        gb = obj["Storage_GB"]

        requests_count = obj["Requests_Per_Month"]

        storage_item = self.get_price_item(
            "Storage",
            product="General Block Blob v2",
            sku="Hot LRS",
            meter_contains="Data Stored"
        )

        request_item = self.get_price_item(
            "Storage",
            product="General Block Blob v2",
            sku="Hot LRS",
            meter_contains="Write Operations"
        )

        if storage_item is None or request_item is None:
            self._record_missing("Blob Storage", "Hot LRS storage/operations")
            return

        storage_price = storage_item["unitPrice"]

        # Operations are priced per 10K, so convert to price per request
        request_price = request_item["unitPrice"] / self.unit_divisor(
            request_item.get("unitOfMeasure")
        )

        storage_cost = gb * storage_price

        request_cost = requests_count * request_price

        cost = storage_cost + request_cost

        self.total_cost += cost

        self.breakdown.append({

            "service": "Blob Storage",

            "inputs": {
                "Storage (GB)": gb,
                "Requests": requests_count
            },

            "pricing": {
                "Storage Price/GB": storage_price,
                "Request Price": request_price,
                "Source": "Azure Retail Pricing API"
            },

            "formula":
                "(Storage × Price/GB) + (Requests × Request Price)",

            "calculation":
                f"({gb} × {storage_price}) + ({requests_count} × {request_price}) = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "Blob Storage cost consists of storage capacity charges and transaction charges."
        })

    ###############################################################
    # Azure Files
    ###############################################################

    def calculate_file_storage(self, file):

        gb = file["Storage_GB"]

        price = self.get_price(
            "Storage",
            product="Premium Files",
            sku="Premium LRS",
            meter_contains="Provisioned"
        )

        if price is None:
            self._record_missing("Azure Files", "Premium LRS Provisioned")
            return

        cost = gb * price

        self.total_cost += cost

        self.breakdown.append({

            "service": "Azure Files",

            "inputs": {
                "Storage (GB)": gb
            },

            "pricing": {
                "Price per GB": price,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "Storage × Price per GB",

            "calculation":
                f"{gb} × {price} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "Azure Files is billed according to the provisioned storage capacity."
        })

    ###############################################################
    # Load Balancer
    ###############################################################

    def calculate_load_balancer(self, lb):

        bandwidth = lb["Bandwidth_Mbps"]

        base_item = self.get_price_item(
            "Load Balancer",
            sku="Standard",
            meter_contains="Included LB Rules"
        )

        data_item = self.get_price_item(
            "Load Balancer",
            sku="Standard",
            meter_contains="Data Processed"
        )

        if base_item is None or data_item is None:
            self._record_missing("Standard Load Balancer", "Standard rules/data processed")
            return

        base_price = base_item["unitPrice"] * MONTHLY_HOURS  # hourly -> monthly

        data_processed_tb = bandwidth / 1024

        data_price_per_gb = data_item["unitPrice"]

        data_price = data_processed_tb * 1024 * data_price_per_gb

        cost = base_price + data_price

        self.total_cost += cost

        self.breakdown.append({

            "service": "Standard Load Balancer",

            "inputs": {
                "Bandwidth (Mbps)": bandwidth
            },

            "pricing": {
                "Base Price (Monthly)": round(base_price, 4),
                "Data Charge/GB": data_price_per_gb,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "Base Price + (Bandwidth/1024 × 1024 × Data Charge per GB)",

            "calculation":
                f"{base_price:.4f} + ({bandwidth}/1024 × 1024 × {data_price_per_gb}) = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "Azure Load Balancer pricing includes a fixed monthly charge plus data processing charges."
        })

    ###############################################################
    # ExpressRoute (FastConnect equivalent)
    ###############################################################

    def calculate_express_route(self, conn):

        hours = conn["Port_Hours"]

        hourly_price = self.get_price(
            "ExpressRoute",
            meter_contains="Port"
        )

        if hourly_price is None:
            self._record_missing("ExpressRoute", "Port hours")
            return

        cost = hours * hourly_price

        self.total_cost += cost

        self.breakdown.append({

            "service": "ExpressRoute",

            "inputs": {
                "Port Hours": hours
            },

            "pricing": {
                "Hourly Price": hourly_price,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "Port Hours × Hourly Price",

            "calculation":
                f"{hours} × {hourly_price} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "ExpressRoute is billed based on the active port hours."
        })

    ###############################################################
    # API Management
    ###############################################################

    def calculate_api_management(self, api):

        calls = api["API_Calls_Million"]

        item = self.get_price_item(
            "API Management",
            sku="Consumption",
            meter_contains="Calls"
        )

        if item is None:
            self._record_missing("API Management", "Consumption calls")
            return

        # Convert price to "per one million calls" using the API's unit
        cost_per_million = item["unitPrice"] * (
            1_000_000 / self.unit_divisor(item.get("unitOfMeasure"))
        )

        cost = calls * cost_per_million

        self.total_cost += cost

        self.breakdown.append({

            "service": "API Management",

            "inputs": {
                "API Calls (Million)": calls
            },

            "pricing": {
                "Cost per Million": cost_per_million,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "API Calls × Cost per Million",

            "calculation":
                f"{calls} × {cost_per_million} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "Azure API Management pricing is calculated according to the number of API calls."
        })

    ###############################################################
    # Main Calculator
    ###############################################################

    def calculate(self, requirements):

        for service in requirements:

            category = service["category"]

            if category == "Compute":

                for vm in service["details"]:
                    self.calculate_compute(vm)

            elif category == "Storage":

                for item in service["details"]:

                    if item["Type"] == "Block":
                        self.calculate_block_storage(item)

                    elif item["Type"] == "Object":
                        self.calculate_object_storage(item)

                    elif item["Type"] == "File":
                        self.calculate_file_storage(item)

            elif category == "Flexible Load Balancer":

                for lb in service["details"]:
                    self.calculate_load_balancer(lb)

            elif category == "Networking - FastConnect":

                for fc in service["details"]:
                    self.calculate_express_route(fc)

            elif category == "Application Development - API Management":

                for api in service["details"]:
                    self.calculate_api_management(api)

        return {
            "cloud": "Azure",

            "currency": "USD",

            "total_monthly_cost": round(self.total_cost, 2),

            "summary": {
                "pricing_model": "Azure Retail Pricing API",
                "calculation_basis": "Pay-as-you-go",
                "billing_period": "Monthly",
                "monthly_hours_assumed": MONTHLY_HOURS,
                "formula": "Total Cost = Sum of all individual service monthly costs"
            },

            "warnings": self.warnings,

            "breakdown": self.breakdown
        }

    ###############################################################
    # Gemini: reads the BOM, fetches prices from the Azure API,
    # and calculates the cost
    ###############################################################

    def calculate_with_gemini(self, requirements):
        """Gemini gets the BOM + a price-search tool backed by the Azure
        Retail Pricing API. Falls back to calculate() if Gemini fails."""

        try:
            from google import genai
            from google.genai import types

            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")

            # Tool that Gemini calls to get real prices (reuses _fetch + cache)
            def search_azure_prices(
                service_name: str,
                arm_sku: str = "",
                product: str = "",
                sku: str = "",
                meter_contains: str = "",
            ) -> list:
                """Search the Azure Retail Pricing API (region and Consumption
                pricing are applied automatically). Returns matching price
                items with unitPrice (USD), unitOfMeasure and tierMinimumUnits.

                Args:
                    service_name: e.g. 'Virtual Machines', 'Storage',
                        'Load Balancer', 'ExpressRoute', 'API Management'.
                    arm_sku: armSkuName, e.g. 'Standard_D8s_v5'. Optional.
                    product: productName, e.g. 'Premium SSD Managed Disks'. Optional.
                    sku: skuName, e.g. 'P10 LRS' or 'Hot LRS'. Optional.
                    meter_contains: text that must appear in meterName. Optional.
                """

                filter_query = (
                    f"serviceName eq '{service_name}'"
                    f" and armRegionName eq '{REGION}'"
                    f" and priceType eq 'Consumption'"
                )

                if arm_sku:
                    filter_query += f" and armSkuName eq '{arm_sku}'"
                if product:
                    filter_query += f" and productName eq '{product}'"
                if sku:
                    filter_query += f" and skuName eq '{sku}'"

                results = []

                for item in self._fetch(filter_query):

                    text = " ".join([
                        item.get("productName", ""),
                        item.get("skuName", ""),
                        item.get("meterName", ""),
                    ]).lower()

                    if any(w in text for w in ("spot", "low priority", "windows")):
                        continue

                    if meter_contains and meter_contains.lower() not in item.get("meterName", "").lower():
                        continue

                    if item.get("unitPrice", 0) <= 0:
                        continue

                    results.append({
                        "productName": item.get("productName"),
                        "skuName": item.get("skuName"),
                        "armSkuName": item.get("armSkuName"),
                        "meterName": item.get("meterName"),
                        "unitOfMeasure": item.get("unitOfMeasure"),
                        "unitPrice": item.get("unitPrice"),
                        "tierMinimumUnits": item.get("tierMinimumUnits", 0),
                    })

                results.sort(key=lambda i: i["tierMinimumUnits"])
                return results[:20]  # keep the tool response small

            client = genai.Client(api_key=api_key)

            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents="BOM requirements:\n" + json.dumps(requirements, indent=2),
                config=types.GenerateContentConfig(
                    system_instruction=GEMINI_SYSTEM_PROMPT,
                    tools=[search_azure_prices],
                    temperature=0,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        maximum_remote_calls=40
                    ),
                ),
            )

            # Strip ```json fences if Gemini adds them
            text = re.sub(r"^```(?:json)?|```$", "", (response.text or "").strip(), flags=re.M).strip()
            data = json.loads(text)

            breakdown = data["breakdown"]
            if not isinstance(breakdown, list):
                raise ValueError("Gemini returned an invalid breakdown")

        except Exception as e:
            self.warnings.append(
                f"Gemini calculation failed ({e}); used rule-based calculation instead"
            )
            return self.calculate(requirements)

        self.breakdown = breakdown
        self.warnings.extend(data.get("warnings", []))

        # Re-add the total in Python so the LLM never does the final sum
        self.total_cost = sum(float(b.get("monthly_cost", 0) or 0) for b in breakdown)

        return {
            "cloud": "Azure",

            "currency": "USD",

            "total_monthly_cost": round(self.total_cost, 2),

            "summary": {
                "pricing_model": "Azure Retail Pricing API (via Gemini)",
                "calculation_basis": "Pay-as-you-go",
                "billing_period": "Monthly",
                "monthly_hours_assumed": MONTHLY_HOURS,
                "formula": "Total Cost = Sum of all individual service monthly costs"
            },

            "warnings": self.warnings,

            "breakdown": self.breakdown
        }
