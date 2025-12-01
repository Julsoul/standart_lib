# standard_lib: Cross-Chain Bridge Event Listener

This repository contains a Python script that simulates a critical component of a cross-chain bridge: the **Event Listener and Relayer**. The script monitors a smart contract on a source blockchain (e.g., Ethereum), detects specific events indicating a user's intent to transfer assets, and relays this information to the destination chain's infrastructure to trigger the corresponding action (e.g., minting tokens).

## Concept

A cross-chain bridge allows users to move assets or data from one blockchain to another. A common mechanism is the "lock-and-mint" model, which typically involves these steps:

1.  **Lock**: A user locks tokens in a smart contract on the source chain (e.g., locking ETH on Ethereum).
2.  **Event Emission**: The smart contract emits an event (`TokensLocked`) containing details of the lock (sender, recipient, amount, destination chain).
3.  **Listen & Verify**: Off-chain services, called listeners or relayers, constantly monitor the source chain for these events.
4.  **Relay & Mint**: Upon detecting a valid event, the relayer submits a signed transaction to the destination chain to mint a corresponding amount of a pegged asset (e.g., minting WETH on another chain).

This script simulates steps 3 and 4. It acts as the off-chain listener that ensures events on the source chain are securely and reliably relayed.

## Code Architecture

The script is designed with a modular, object-oriented architecture to separate concerns and enhance maintainability. The core components are:

-   `BlockchainConnector`: An interface to the source blockchain. It uses the `web3.py` library to connect to an RPC node (like Infura or Alchemy), instantiate the bridge contract using its address and ABI, and query for event logs within specific block ranges.
    ```python
    # Example of contract instantiation in the connector
    from web3 import Web3
    
    # Assuming web3_client and contract_abi_json are loaded
    contract = web3_client.eth.contract(
        address="0x...",
        abi=contract_abi_json
    )
    ```

-   `EventProcessor`: Its primary responsibility is to take raw event data from the `BlockchainConnector`, parse it into a clean, structured format, and perform basic validation. This separation ensures that business logic is decoupled from the data-fetching mechanism. For example, it might populate a `BridgeTransferEvent` dataclass:
    ```python
    # (e.g., in a file like models.py)
    from dataclasses import dataclass

    @dataclass
    class BridgeTransferEvent:
        transaction_hash: str
        sender: str
        recipient: str
        amount: int
        destination_chain_id: int
    ```

-   `TransactionRelayer`: This component simulates the final step: relaying the event. It takes a parsed event and sends an HTTP POST request (using the `requests` library) to a mock API endpoint. In a real-world scenario, this endpoint would belong to a service responsible for signing and broadcasting the transaction on the destination chain.

-   `BridgeEventListener`: The main orchestrator. It initializes all other components, manages the application's state (i.e., the last block number it has processed), and runs the main infinite loop. This loop periodically queries for new blocks, fetches events, processes them, and hands them off to the relayer.

### Architectural Flow

```
+-----------------------+
| BridgeEventListener   | (Main Loop)
+-----------+-----------+
            | 1. Get Block Numbers
            v
+-----------------------+
| BlockchainConnector   |
| (web3.py)             |--- 2. Fetch Event Logs ---
+-----------------------+                            |
            | 3. Process Events                      v
            v
+-----------------------+      +--------------------+
| EventProcessor        |----->| BridgeTransferEvent|
| (Parse & Validate)    |      | (Dataclass)        |
+-----------------------+      +--------------------+
            | 4. Relay Transaction                   ^
            v                                        |
+-----------------------+      5. POST to API        |
| TransactionRelayer    |----------------------------+
| (requests)            |
+-----------------------+
```

## How It Works

1.  **Initialization**: The script starts by loading configuration from environment variables (using a `.env` file), including the RPC URL, contract address, and ABI path.

2.  **State Management**: It checks for a `last_processed_block.dat` file.
    -   If found, it resumes scanning from that block number, minus a `REORG_SAFETY_MARGIN` to handle potential blockchain reorganizations.
    -   If not found, it starts scanning from the current latest block to avoid processing the entire chain history on its first run.

3.  **Polling Loop**: The `BridgeEventListener` enters an infinite loop:
    a.  It fetches the latest block number from the chain.
    b.  It compares this with its last processed block to determine the range of new blocks to scan.
    c.  To avoid overwhelming the RPC node, it processes blocks in manageable chunks (e.g., 100 blocks at a time).
    d.  It calls `BlockchainConnector` to get all `TokensLocked` events within that chunk.
    e.  Each raw event is passed to the `EventProcessor` to be parsed and validated.
    f.  Valid, parsed events are then sent to the `TransactionRelayer`, which sends the data to an external API.
    g.  After scanning a chunk, it updates `last_processed_block.dat` to persist its progress.
    h.  It waits for a configured poll interval before starting the next iteration.

4.  **Error Handling**: The script includes robust error handling for network issues, invalid data, and API failures, logging them appropriately without halting the service.

## Usage

**1. Clone the repository:**
```bash
git clone https://github.com/your-username/standard_lib.git
cd standard_lib
```

**2. Create a virtual environment and install dependencies:**
```bash
python -m venv venv
source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
pip install -r requirements.txt
```

**3. Add the Contract ABI:**

Create a file named `bridge_abi.json` in the root directory. Paste the JSON ABI of the smart contract you want to monitor into this file. The ABI is required to decode event logs.

Your project structure should look like this:
```
standard_lib/
├── .env
├── script.py
├── bridge_abi.json
├── requirements.txt
└── venv/
```

**4. Set up your environment variables:**

Create a file named `.env` in the root directory and add the following, replacing the placeholder values:

```env
# Get this from a service like Infura, Alchemy, or your own node.
ETHEREUM_RPC_URL="https://sepolia.infura.io/v3/<YOUR_API_KEY>"

# The address of the bridge smart contract to monitor.
# NOTE: The address below is a placeholder. Replace it with a real one.
BRIDGE_CONTRACT_ADDRESS="0x1234567890123456789012345678901234567890"

# Path to the JSON file containing the bridge contract's ABI.
BRIDGE_CONTRACT_ABI_PATH="./bridge_abi.json"

# (Optional) The API endpoint for the relayer service.
# Defaults to a public mock API for testing.
RELAYER_API_URL="https://httpbin.org/post"

# (Optional) How often to check for new blocks, in seconds.
POLL_INTERVAL=15

# (Optional) Max number of blocks to scan in one go.
BLOCK_CHUNK_SIZE=100
```

**5. Run the script:**

```bash
python script.py
```

### Expected Output

The console will show logs indicating the script's activity.

```
2023-10-27 15:30:00 - INFO - Successfully connected to RPC at https://sepolia.infura.io/v3/...
2023-10-27 15:30:01 - INFO - Resuming from block 4850120 (last saved: 4850130, reorg margin: 10)
2023-10-27 15:30:01 - INFO - Starting Cross-Chain Bridge Event Listener...
2023-10-27 15:30:16 - INFO - Scanning blocks from 4850121 to 4850221...
2023-10-27 15:30:18 - INFO - Found 1 'TokensLocked' events between blocks 4850121 and 4850221.
2023-10-27 15:30:18 - INFO - Relaying transaction 0xabc...def to https://httpbin.org/post
2023-10-27 15:30:19 - INFO - Successfully relayed tx 0xabc...def. Response: { ... }
2023-10-27 15:30:34 - INFO - No new blocks to process. Current: 4850221. Waiting...
```