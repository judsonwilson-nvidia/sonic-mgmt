import itertools
import json
import logging
import pytest
import re
import time
from typing import Optional

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.helpers.sonic_db import AsicDbCli, SonicDbCli

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("t0")
]

N_ROUTES = 40000
N_ROUTES_ADD_BATCH = 500
N_ROUTES_DEL_BATCH = 500
ROUTE_INSTALL_TIMEOUT = 1200  # seconds
ROUTE_REMOVAL_TIMEOUT = 2000  # seconds

class TestAddStaticRoutes:
    def _remove_all_routes(self, duthost, assert_cpu_mem: bool = False) -> None:
        """
        Remove all static routes from the DUT using direct Redis operations.
        """
        num_routes = self._count_static_routes_in_summary(duthost)
        if num_routes == 0:
            logger.info("No static routes to remove.")
            return
        
        logger.info(f"Removing all {num_routes} static routes using Redis operations...")

        initial_time = time.time()

        # Initialize CONFIG_DB connection
        config_db = SonicDbCli(duthost, database='CONFIG_DB')
        
        # Get all static route keys
        try:
            route_keys = config_db.get_keys("STATIC_ROUTE*", raise_error_when_not_found=False)
        except Exception as e:
            logger.error(f"Failed to get static route keys: {e}")
            route_keys = []
        
        if not route_keys:
            logger.info("No static route keys found in CONFIG_DB.")
            return
        
        logger.info(f"Found {len(route_keys)} static route keys to remove")
        
        # Remove static route keys in batches using redis-cli UNLINK
        # CONFIG_DB is database number 4
        # UNLINK is async and more efficient than DEL for bulk operations
        total_deleted = 0
        for i in range(0, len(route_keys), N_ROUTES_DEL_BATCH):
            batch = route_keys[i:i + N_ROUTES_DEL_BATCH]
            # Build the UNLINK command with all keys in this batch
            keys_args = ' '.join(f"'{key}'" for key in batch)
            duthost.shell(f"redis-cli -n 4 UNLINK {keys_args}", module_ignore_errors=True)
            total_deleted += len(batch)
            logger.info(f"Deleted {total_deleted}/{len(route_keys)} keys from CONFIG_DB")
        
        logger.info(f"Deleted all {len(route_keys)} keys from CONFIG_DB")
        delete_config_db_time = time.time()
        logger.info(f"Time taken to delete {len(route_keys)} keys from CONFIG_DB: {delete_config_db_time - initial_time} seconds")

        if assert_cpu_mem:
            # At this point, the routing stack should be working hard to actually remove the routes,
            # and CPU Usage should be higher. mgmtd uses a significant amount of a core, and others.
            # Observed cores is around 1.5 - 2.
            self._assert_cpu_mem(duthost, cpu_cores_min=0.5, cpu_cores_max=4, mem_max=0.2)
        else:
            logger.info("Skipping CPU and memory assertions")
        
        # Wait for all routes to be removed from the routing table
        logger.info(f"Waiting for {num_routes} static routes to be removed from routing table...")
        def check_routes_removed_from_summary():
            current_count = self._count_static_routes_in_summary(duthost)
            logger.info(f"Current route count: {current_count} (waiting for 0)")
            return current_count == 0
        pytest_assert(
            wait_until(timeout=ROUTE_REMOVAL_TIMEOUT, interval=5, delay=0, condition=check_routes_removed_from_summary),
            f"Timeout: Expected 0 routes but some remain in summary after {ROUTE_REMOVAL_TIMEOUT} seconds"
        )
        
        logger.info("All static routes successfully removed from summary")
        remove_summary_time = time.time()
        logger.info(f"Time taken to remove {num_routes} routes from CONFIG_DB: {delete_config_db_time - initial_time} seconds")
        logger.info(f"Additional time taken to remove {num_routes} routes from summary: {remove_summary_time - delete_config_db_time} seconds")

        def check_routes_removed_from_asic():
            current_count = self._count_routes_in_asic(duthost)
            logger.info(f"Current route count in ASIC_DB: {current_count} (waiting for 0)")
            return current_count == 0
        pytest_assert(
            wait_until(timeout=ROUTE_REMOVAL_TIMEOUT, interval=5, delay=0, condition=check_routes_removed_from_asic),
            f"Timeout: Expected 0 routes but some remain after {ROUTE_REMOVAL_TIMEOUT} seconds"
        )

        logger.info("All static routes successfully removed from ASIC_DB")
        remove_asic_time = time.time()
        logger.info(f"Time taken to remove {num_routes} routes from CONFIG_DB: {delete_config_db_time - initial_time} seconds")
        logger.info(f"Additional time taken to remove {num_routes} routes from summary: {remove_summary_time - delete_config_db_time} seconds")
        logger.info(f"Additional time taken to remove {num_routes} routes from ASIC_DB: {remove_asic_time - remove_summary_time} seconds")
        logger.info(f"Total time taken to remove {num_routes} routes from CONFIG_DB, summary, and ASIC_DB: {remove_asic_time - initial_time} seconds")

    def _count_static_routes_in_summary(self, duthost) -> int:
        """
        Count the number of static routes on the DUT using 'show ip route summary'.
        Returns the count from the static routes line, or 0 if no static routes exist.
        """
        logger.info("Counting static routes...")
        command_output = duthost.shell("show ip route summary", module_ignore_errors=True)
        
        if command_output['rc'] != 0:
            logger.error(f"Failed to get route summary: {command_output['stderr']}")
            return 0
        
        # Parse output to find the "static" line
        # Example line: "static          6000" or "Static          6000"
        route_count = 0
        for line in command_output['stdout_lines']:
            # Look for a line containing "static" (case-insensitive)
            if 'static' in line.lower():
                # Extract the number from the line
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        route_count = int(parts[1])
                        break
                    except ValueError:
                        logger.warning(f"Could not parse static route count from line: {line}")
                        continue
        
        logger.info(f"Found {route_count} static routes from summary")
        return route_count

    def _count_routes_in_asic(self, duthost) -> int:
        """
        Count routes in ASIC_DB where the prefix starts with 10.7 through 10.99.
        Returns the count of matching route entries.
        """
        logger.info("Counting routes in ASIC_DB with prefix 10.7-10.99...")
        
        # Initialize ASIC_DB connection
        asic_db = AsicDbCli(duthost)
        
        # Get all route entry keys
        try:
            route_keys = asic_db.get_keys("ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY*", raise_error_when_not_found=False)
        except Exception as e:
            logger.error(f"Failed to get route keys from ASIC_DB: {e}")
            return 0
        
        if not route_keys:
            logger.info("No route entries found in ASIC_DB")
            return 0
        
        # Count routes matching the prefix pattern 10.7-10.99
        # Route keys contain JSON with "dest" field like "10.7.0.1/32"
        matching_count = 0
        # Pattern to match 10.7.x.x through 10.99.x.x
        prefix_pattern = re.compile(r'"dest"\s*:\s*"10\.([7-9]|[1-9][0-9])\.\d+\.\d+')
        
        for key in route_keys:
            # Check if the key contains a matching prefix
            if prefix_pattern.search(key):
                matching_count += 1
        
        logger.info(f"Found {matching_count} routes in ASIC_DB with prefix 10.7-10.99")
        return matching_count

    def _assert_cpu_mem(self, duthost, cpu_cores_min: Optional[float] = None, 
                        cpu_cores_max: Optional[float] = None, mem_max: Optional[float] = None) -> None:
        """
        Assert that system CPU and memory usage are within specified bounds.
        
        Args:
            duthost: DUT host object
            cpu_cores_min: Minimum CPU cores threshold (e.g., 2.0 = 100% of two cores) (None to skip check)
            cpu_cores_max: Maximum CPU cores threshold (e.g., 2.0 = 100% of two cores) (None to skip check)
            mem_max: Maximum memory fraction threshold, 0.0-1.0 (None to skip check)
        
        Raises:
            AssertionError: If CPU or memory are out of bounds
        """
        logger.info(f"Checking system CPU and memory usage...")
        
        # Get number of CPU cores. Fail on any error or unexpected output.
        num_cores_output = duthost.shell("nproc", module_ignore_errors=True)
        if num_cores_output['rc'] != 0:
            raise Exception(f"Failed to get CPU core count: {num_cores_output.get('stderr', '')}")
        try:
            num_cores = int(num_cores_output['stdout'].strip())
        except ValueError as e:
            raise Exception(f"Could not parse nproc output: {num_cores_output['stdout']}, error: {e}")
        if num_cores <= 0:
            raise Exception(f"Invalid CPU core count: {num_cores}")
        logger.info(f"System has {num_cores} CPU cores")
        
        # Convert memory threshold to percentage for comparison
        mem_max_pct = mem_max * 100 if mem_max is not None else None
        
        # Run top with 2 iterations, 5 second delay (similar to test_snmp_cpu)
        # First iteration is often inaccurate, so we discard it
        output = duthost.shell(
            "top -bn2 -d5 | awk '/^top -/ { p=!p } { if (!p) print }'",
            module_ignore_errors=True
        )
        
        if output['rc'] != 0:
            logger.error(f"Failed to get top output: {output.get('stderr', '')}")
            return
        
        # Parse CPU usage from Cpu line
        # Example: "%Cpu(s):  5.2 us,  2.1 sy,  0.0 ni, 92.2 id,  0.5 wa,  0.0 hi,  0.0 si,  0.0 st"
        cpu_usage_pct = None
        cpu_usage_cores = None
        mem_usage = None
        
        for line in output['stdout_lines']:
            if 'Cpu' in line or 'CPU' in line:
                # Extract idle percentage (field 8 typically) and calculate usage
                parts = line.split()
                for i, part in enumerate(parts):
                    if 'id' in part:  # idle
                        try:
                            idle_str = parts[i-1].rstrip(',')
                            idle_percent = float(idle_str)
                            cpu_usage_pct = round(100.0 - idle_percent, 1)
                            # Convert percentage to cores immediately
                            cpu_usage_cores = round((cpu_usage_pct / 100) * num_cores, 2)
                            break
                        except (ValueError, IndexError) as e:
                            logger.warning(f"Could not parse CPU idle from: {line}, error: {e}")
            
            # Parse memory usage from memory line
            # Example: "KiB Mem : 16324508 total,  2045732 free,  8234556 used,  6044220 buff/cache"
            # or "MiB Mem :  15942.9 total,   1997.8 free,   8041.2 used,   5903.9 buff/cache"
            if 'Mem' in line and 'total' in line:
                try:
                    parts = line.split()
                    total_idx = parts.index('total,')
                    used_idx = parts.index('used,')
                    total = float(parts[total_idx - 1])
                    used = float(parts[used_idx - 1])
                    mem_usage = round((used / total) * 100.0, 1)
                except (ValueError, IndexError) as e:
                    logger.warning(f"Could not parse memory from: {line}, error: {e}")
        
        # Log summary
        summary_parts = []
        if cpu_usage_cores is not None:
            summary_parts.append(f"CPU: {cpu_usage_cores:.2f} cores ({cpu_usage_pct}% of {num_cores} cores)")
        if mem_usage is not None:
            summary_parts.append(f"Memory: {mem_usage}%")
        
        logger.info(f"System usage: {', '.join(summary_parts)}")
        
        # Check thresholds and build failure message
        failures = []
        
        if cpu_usage_cores is not None:
            cpu_cores_min_pct = (cpu_cores_min / num_cores) * 100 if cpu_cores_min is not None else None
            cpu_cores_max_pct = (cpu_cores_max / num_cores) * 100 if cpu_cores_max is not None else None
            
            if cpu_cores_min is not None and cpu_usage_cores < cpu_cores_min:
                failures.append(f"CPU usage {cpu_usage_cores:.2f} cores ({cpu_usage_pct}% of {num_cores} cores) is below minimum threshold of {cpu_cores_min} cores ({cpu_cores_min_pct:.1f}%)")
                logger.error(failures[-1])
            if cpu_cores_max is not None and cpu_usage_cores > cpu_cores_max:
                failures.append(f"CPU usage {cpu_usage_cores:.2f} cores ({cpu_usage_pct}% of {num_cores} cores) exceeds maximum threshold of {cpu_cores_max} cores ({cpu_cores_max_pct:.1f}%)")
                logger.error(failures[-1])
        else:
            logger.warning("Could not determine CPU usage from top output")
        
        if mem_usage is not None:
            if mem_max_pct is not None and mem_usage > mem_max_pct:
                failures.append(f"Memory {mem_usage}% exceeds maximum threshold {mem_max_pct}% ({mem_max})")
                logger.error(failures[-1])
        else:
            logger.warning("Could not determine memory usage from top output")
        
        # Assert if any thresholds were violated
        if failures:
            pytest.fail(f"System resource usage out of bounds ({len(failures)} violations):\n" 
                        + "\n".join(f"  - {f}" for f in failures))
        
        logger.info(f"All thresholds satisfied")

    def test_static_route_add(self, duthost) -> None:
        """
        Test adding and removing 40k static routes.
        """

        # Observed cores is around 1.5, and highly noisy.
        # TODO: More investigation is needed to understand the CPU usage here.
        self._assert_cpu_mem(duthost, cpu_cores_max=3.0, mem_max=0.2)

        logger.info(f"Start of test: Emptying routes...")
        self._remove_all_routes(duthost, assert_cpu_mem=False)

        logger.info(f"Start of test: Adding {N_ROUTES} static routes...")

        start_time = time.time()
        def gen_ips():
            """Generate IPs in the range 10.7.1.1 to 10.99.254.254."""
            for i in range(7, 100):  # 100 to include 10.99.x.x
                for j in range(1, 255):
                    for k in range(1, 255):
                        yield f"10.{i}.{j}.{k}"
            raise Exception("Not enough IPs")

        ip_generator = gen_ips()

        routes_added = 0
        routes_remaining = N_ROUTES
        iteration = 0

        while routes_remaining > 0:
            batch_size = min(routes_remaining, N_ROUTES_ADD_BATCH)
            
            # Get next batch of IPs
            batch_ips = list(itertools.islice(ip_generator, batch_size))
            
            # Build STATIC_ROUTE config dictionary for this batch
            static_routes_config = {
                f"default|{ip}/32": {
                    "blackhole": "false",
                    "distance": "0",
                    "ifname": "",
                    "nexthop": "10.200.0.1",
                    "nexthop-vrf": "default"
                }
                for ip in batch_ips
            }
            
            # Create complete config JSON
            config_json = {
                "STATIC_ROUTE": static_routes_config
            }
            
            # Write to temp file on DUT
            config_json_str = json.dumps(config_json, indent=4)
            tmpfile = f"/tmp/static_routes_batch_{iteration}.json"
            
            logger.info(f"Loading batch {iteration} with {len(batch_ips)} routes (first 3: {batch_ips[:3]})")
            
            try:
                # Copy JSON content to DUT
                duthost.copy(content=config_json_str, dest=tmpfile)
                
                # Load config using 'config load -y'
                output = duthost.shell(f"config load -y {tmpfile}", module_ignore_errors=True)
                
                # Check for success
                if output['rc'] != 0:
                    logger.error(f"Config load failed: {output.get('stderr', '')}")
                    pytest.fail(f"Failed to load config batch {iteration}")
                
                logger.info(f"Config load output: {output.get('stdout', '')}")
            finally:
                # Clean up temp file
                duthost.shell(f"rm -f {tmpfile}", module_ignore_errors=True)
            
            routes_added += batch_size
            routes_remaining = N_ROUTES - routes_added
            iteration += 1
            
            logger.info(f"Iteration {iteration}: Added {routes_added} routes, {routes_remaining} remaining")
            current_programming_time = time.time()
            logger.info(f"Current time taken: {current_programming_time - start_time} seconds")

        end_programming_time = time.time()
        logger.info(f"Time taken to request add {N_ROUTES} routes: {end_programming_time - start_time} seconds")

        # At this point, the routing stack should be working hard to actually add the routes,
        # and CPU Usage should be higher. mgmtd uses a significant amount of a core, and others.
        # Observed cores is around 1.5 - 2.
        self._assert_cpu_mem(duthost, cpu_cores_min=0.5, cpu_cores_max=4, mem_max=0.2)

        # Wait for all routes to be installed in summary
        logger.info(f"Waiting for {N_ROUTES} static routes to be installed in summary...")
        def check_routes_in_summary():
            current_count = self._count_static_routes_in_summary(duthost)
            logger.info(f"Current route count in summary: {current_count}/{N_ROUTES}")
            return current_count >= N_ROUTES
        pytest_assert(
            wait_until(timeout=ROUTE_INSTALL_TIMEOUT, interval=5, delay=0, condition=check_routes_in_summary),
            f"Timeout: Expected {N_ROUTES} routes installed in summary after {ROUTE_INSTALL_TIMEOUT} seconds"
        )
        end_summary_time = time.time()
        logger.info(f"Time taken to add {N_ROUTES} to config: {end_programming_time - start_time} seconds")
        logger.info(f"Additional time taken until verified in summary: {end_summary_time - end_programming_time} seconds")

        # Wait for all routes to be installed in ASIC_DB
        def check_routes_in_asic():
            current_count = self._count_routes_in_asic(duthost)
            logger.info(f"Current route count in ASIC_DB: {current_count}/{N_ROUTES}")
            return current_count >= N_ROUTES
        pytest_assert(
            wait_until(timeout=ROUTE_INSTALL_TIMEOUT, interval=5, delay=0, condition=check_routes_in_asic),
            f"Timeout: Expected {N_ROUTES} routes installed in ASIC_DB after {ROUTE_INSTALL_TIMEOUT} seconds"
        )

        end_asic_time = time.time()
        logger.info(f"Time taken to add {N_ROUTES} to config: {end_programming_time - start_time} seconds")
        logger.info(f"Additional time taken until verified in summary: {end_summary_time - end_programming_time} seconds")
        logger.info(f"Additional time taken until verified in ASIC_DB: {end_asic_time - end_summary_time} seconds")
        total_install_time = end_asic_time - start_time
        logger.info(f"Total time taken to add {N_ROUTES} to config and verify in summary and ASIC_DB: {total_install_time} seconds")

        pytest_assert(
            total_install_time < ROUTE_INSTALL_TIMEOUT,
            f"Timeout: Expected {N_ROUTES} routes installed in ASIC_DB within {ROUTE_INSTALL_TIMEOUT} seconds. Took {total_install_time} seconds."
        )

        # At this point, the heavily lifing of adding routes should be done (except maybe for the SDK to program the asic).
        # CPU Usage should be lower. Observed cores is around 1.
        self._assert_cpu_mem(duthost, cpu_cores_max=3, mem_max=0.2)

        logger.info("Starting to remove routes...")
        remove_routes_start_time = time.time()
        self._remove_all_routes(duthost, assert_cpu_mem=True)
        end_remove_routes_time = time.time()
        logger.info("Finished removing routes...")
        total_remove_time = end_remove_routes_time - remove_routes_start_time
        logger.info(f"Time taken to remove {N_ROUTES} routes: {total_remove_time} seconds")

        pytest_assert(
            total_remove_time < ROUTE_REMOVAL_TIMEOUT,
            f"Timeout: Expected {N_ROUTES} routes removed within {ROUTE_REMOVAL_TIMEOUT} seconds. Took {total_remove_time} seconds."
        )
