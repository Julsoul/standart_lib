import os
import json
import time
import logging
from dataclasses import dataclass

import requests
from web3 import Web3
from web3.exceptions import BlockNotFound
from dotenv import load_dotenv

# --- Configuration & Setup ---

load_dotenv() # Load environment variables from .env file

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- Constants & ABI ---

# This is a sample ABI for a generic bridge contract.
# It includes an event that the listener will be looking for.
BRIDGE_CONTRACT_ABI = json.dumps([
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "sender", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "recipient", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"},
            {"indexed": True, "internalType": "uint256", "name": "destinationChainId", "type": "uint256"},
            {"indexed": False, "internalType": "bytes32", "name": "sourceTxHash", "type": "bytes32"}
        ],
        "name": "TokensLocked",
        "type": "event"
    }
])

STATE_FILE = 'last_processed_block.dat'
# Number of blocks to re-process upon restart to handle potential chain reorgs.
REORG_SAFETY_MARGIN = 10 


# --- Data Structures ---

@dataclass
class BridgeTransferEvent:
    """A structured representation of a cross-chain transfer event."""
    sender: str
    recipient: str
    amount: int
    destination_chain_id: int
    source_tx_hash: str
    block_number: int


# --- Core Components ---

class BlockchainConnector:
    """Handles connection to a blockchain node and contract interaction."""

    def __init__(self, rpc_url: str, contract_address: str):
        """
        Initializes the Web3 provider and contract instance.

        Args:
            rpc_url (str): The URL of the blockchain RPC endpoint.
            contract_address (str): The address of the bridge smart contract.
        """
        try:
            self.web3 = Web3(Web3.HTTPProvider(rpc_url))
            if not self.web3.is_connected():
                raise ConnectionError("Failed to connect to the blockchain RPC.")
            
            self.contract_address = self.web3.to_checksum_address(contract_address)
            self.contract = self.web3.eth.contract(
                address=self.contract_address, abi=BRIDGE_CONTRACT_ABI
            )
            logging.info(f"Successfully connected to RPC at {rpc_url}")
        except Exception as e:
            logging.error(f"Error initializing BlockchainConnector: {e}")
            raise

    def get_latest_block_number(self) -> int:
        """Fetches the most recent block number from the blockchain."""
        try:
            return self.web3.eth.block_number
        except Exception as e:
            logging.error(f"Could not fetch latest block number: {e}")
            return 0

    def get_events_in_range(self, from_block: int, to_block: int) -> list:
        """
        Fetches 'TokensLocked' events within a specified block range.

        Args:
            from_block (int): The starting block number.
            to_block (int): The ending block number.

        Returns:
            list: A list of raw event log objects.
        """
        try:
            event_filter = self.contract.events.TokensLocked.create_filter(
                fromBlock=from_block,
                toBlock=to_block
            )
            events = event_filter.get_all_entries()
            if events:
                logging.info(f"Found {len(events)} 'TokensLocked' events between blocks {from_block} and {to_block}.")
            return events
        except BlockNotFound:
            logging.warning(f"Block range {from_block}-{to_block} not found. The RPC node might not have this data.")
            return []
        except Exception as e:
            logging.error(f"Error fetching events from block {from_block} to {to_block}: {e}")
            return []


class EventProcessor:
    """Parses and validates raw event logs into a structured format."""

    @staticmethod
    def parse_event(event_log: dict) -> BridgeTransferEvent | None:
        """
        Transforms a raw Web3 event log into a BridgeTransferEvent dataclass.

        Args:
            event_log (dict): The raw event log from web3.py.

        Returns:
            BridgeTransferEvent | None: A structured event object, or None if parsing fails.
        """
        try:
            args = event_log['args']
            parsed_event = BridgeTransferEvent(
                sender=args['sender'],
                recipient=args['recipient'],
                amount=args['amount'],
                destination_chain_id=args['destinationChainId'],
                source_tx_hash=event_log['transactionHash'].hex(),
                block_number=event_log['blockNumber']
            )
            # Basic validation
            if not all([parsed_event.sender, parsed_event.recipient, parsed_event.amount > 0]):
                logging.warning(f"Invalid event data found in tx {parsed_event.source_tx_hash}. Skipping.")
                return None
            return parsed_event
        except (KeyError, TypeError) as e:
            logging.error(f"Failed to parse event log due to malformed data: {event_log}. Error: {e}")
            return None


class TransactionRelayer:
    """Simulates relaying a transaction to a destination chain via an API."""

    def __init__(self, relayer_api_url: str):
        """
        Initializes the relayer with the target API endpoint.

        Args:
            relayer_api_url (str): The URL of the relayer service API.
        """
        self.api_url = relayer_api_url
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'StandartLib-Bridge-Relayer/1.0'
        })

    def relay_transaction(self, event: BridgeTransferEvent) -> bool:
        """
        Sends the processed event data to the relayer API.

        Args:
            event (BridgeTransferEvent): The event to be relayed.

        Returns:
            bool: True if the API call was successful (e.g., 2xx status code), False otherwise.
        """
        payload = {
            'source_chain': 'ethereum-sepolia', # Example value
            'destination_chain_id': event.destination_chain_id,
            'data': {
                'sender': event.sender,
                'recipient': event.recipient,
                'amount': str(event.amount), # Send amount as string for precision
                'source_tx_hash': event.source_tx_hash
            }
        }
        logging.info(f"Relaying transaction {event.source_tx_hash} to {self.api_url}")
        try:
            response = self.session.post(self.api_url, json=payload, timeout=15)
            response.raise_for_status() # Raises an HTTPError for bad responses (4xx or 5xx)
            logging.info(f"Successfully relayed tx {event.source_tx_hash}. Response: {response.json()}")
            return True
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to relay tx {event.source_tx_hash}. API error: {e}")
            return False


class BridgeEventListener:
    """The main orchestrator for listening, processing, and relaying events."""

    def __init__(self, config: dict):
        """
        Initializes all components based on the provided configuration.
        """
        self.config = config
        self.connector = BlockchainConnector(
            rpc_url=config['RPC_URL'],
            contract_address=config['BRIDGE_CONTRACT_ADDRESS']
        )
        self.processor = EventProcessor()
        self.relayer = TransactionRelayer(config['RELAYER_API_URL'])
        self.last_processed_block = self._load_last_processed_block()
        self.poll_interval_seconds = config.get('POLL_INTERVAL', 15)

    def _load_last_processed_block(self) -> int:
        """Loads the last processed block number from the state file."""
        try:
            with open(STATE_FILE, 'r') as f:
                block_number = int(f.read().strip())
                # Apply a safety margin to handle potential chain reorgs
                start_block = max(0, block_number - REORG_SAFETY_MARGIN)
                logging.info(f"Resuming from block {start_block} (last saved: {block_number}, reorg margin: {REORG_SAFETY_MARGIN})")
                return start_block
        except (FileNotFoundError, ValueError):
            logging.warning(f"'{STATE_FILE}' not found or invalid. Starting from the latest block.")
            # Fallback to the current block if the file doesn't exist
            return self.connector.get_latest_block_number()

    def _save_last_processed_block(self, block_number: int):
        """Saves the last processed block number to the state file."""
        try:
            with open(STATE_FILE, 'w') as f:
                f.write(str(block_number))
        except IOError as e:
            logging.error(f"Could not save last processed block {block_number} to '{STATE_FILE}': {e}")

    def run(self):
        """Starts the main event listening loop."""
        logging.info("Starting Cross-Chain Bridge Event Listener...")
        while True:
            try:
                latest_block = self.connector.get_latest_block_number()
                if latest_block == 0:
                    logging.warning("Could not get latest block. Retrying...")
                    time.sleep(self.poll_interval_seconds)
                    continue

                # Ensure we don't process blocks in the future
                if self.last_processed_block >= latest_block:
                    logging.info(f"No new blocks to process. Current: {latest_block}. Waiting...")
                    time.sleep(self.poll_interval_seconds)
                    continue
                
                # Define the range of blocks to scan in this iteration
                # Process in chunks to avoid overwhelming the RPC node
                to_block = min(self.last_processed_block + self.config.get('BLOCK_CHUNK_SIZE', 100), latest_block)
                from_block = self.last_processed_block + 1

                logging.info(f"Scanning blocks from {from_block} to {to_block}...")

                raw_events = self.connector.get_events_in_range(from_block, to_block)

                if raw_events:
                    for event_log in raw_events:
                        parsed_event = self.processor.parse_event(event_log)
                        if parsed_event:
                            # For this simulation, we'll try to relay and continue regardless of success.
                            # In a real system, a robust retry queue (e.g., RabbitMQ, Redis) would be used.
                            self.relayer.relay_transaction(parsed_event)
                
                # Update state to the last block we've processed in this batch
                self._save_last_processed_block(to_block)
                self.last_processed_block = to_block

                # If we are far behind the chain tip, process the next chunk immediately
                if to_block < latest_block:
                    time.sleep(1) # Small delay to be nice to the RPC
                else:
                    time.sleep(self.poll_interval_seconds)

            except KeyboardInterrupt:
                logging.info("Shutdown signal received. Exiting gracefully.")
                break
            except Exception as e:
                logging.critical(f"An unexpected error occurred in the main loop: {e}", exc_info=True)
                # Wait before retrying to prevent rapid-fire failures
                time.sleep(self.poll_interval_seconds * 2)


if __name__ == '__main__':
    # --- Main Execution ---
    # Configuration should be loaded from environment variables for security and flexibility.
    app_config = {
        'RPC_URL': os.getenv('ETHEREUM_RPC_URL'),
        'BRIDGE_CONTRACT_ADDRESS': os.getenv('BRIDGE_CONTRACT_ADDRESS'),
        'RELAYER_API_URL': os.getenv('RELAYER_API_URL', 'https://httpbin.org/post'), # Default mock API
        'POLL_INTERVAL': int(os.getenv('POLL_INTERVAL', '15')), # In seconds
        'BLOCK_CHUNK_SIZE': int(os.getenv('BLOCK_CHUNK_SIZE', '100'))
    }

    # Validate essential configuration
    if not all([app_config['RPC_URL'], app_config['BRIDGE_CONTRACT_ADDRESS']]):
        raise ValueError("ETHEREUM_RPC_URL and BRIDGE_CONTRACT_ADDRESS must be set in the .env file.")

    listener = BridgeEventListener(config=app_config)
    listener.run()
