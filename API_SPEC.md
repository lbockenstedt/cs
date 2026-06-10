# Client Simulator (CS) Spoke API Specification

The Client Simulator Spoke is used to generate synthetic network traffic and DNS requests to test infrastructure resilience and security policies.

## Command Set

### Configuration
- **`UPDATE_CONFIG`**
  - **Purpose**: Updates the global simulation profile registry.
  - **Payload**: `{"sim_profiles": { "s0": { "sim_a": "on" }, "s1": { ... } }}`
  - **Response**: `{"status": "SUCCESS", "message": "..."}`
- **`SET_SIMULATION_PROFILE`**
  - **Purpose**: Updates the active profile for the current bucket assigned to this spoke.
  - **Payload**: `{"profile": { "sim_a": "on", "sim_b": "off" }}`
  - **Response**: `{"status": "SUCCESS", "message": "..."}`

### Execution & State
- **`TRIGGER_ITERATION`**
  - **Purpose**: Forces the simulation engine to execute one iteration of the active profile's network behaviors.
  - **Payload**: `{}`
  - **Response**: `{"hostname": "string", "bucket": "string", "active_sims": [], "status": "SUCCESS"}`
- **`GET_SIMULATION_STATE`**
  - **Purpose**: Retrieves the current state of the simulation engine.
  - **Payload**: `{}`
  - **Response**: `{"username": "string", "simulation_id": "string", "config": {...}, "active_simulations": [], "status": "string"}`

## Architecture
- **Bucket Assignment**: Each CS spoke is deterministically assigned to a "bucket" (s0-s9) based on its hostname using a CRC32 hash.
- **Execution Model**: Profiles define which simulation scripts (e.g., `sim_a`, `sim_b`) are enabled. When triggered, the engine executes all enabled simulations.

## Integration Flow
1. **Command Trigger**: Hub sends a signed WebSocket message (e.g., `TRIGGER_ITERATION`).
2. **Execution**: `CSSpoke` calls the `SimulationEngine` to run the active profile.
3. **Simulation**: The engine simulates network activity based on the `sim_profiles` configuration.
4. **Confirmation**: A signed result containing the active simulations and success status is returned to the Hub.
