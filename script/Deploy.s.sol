// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console} from "forge-std/Script.sol";
import {GuardrailVault} from "../src/GuardrailVault.sol";

/// @notice Deploy GuardrailVault against the live HSKSwap WHSK/USDT pool.
///
///   forge script script/Deploy.s.sol:Deploy \
///     --rpc-url https://mainnet.hsk.xyz \
///     --private-key $PRIVATE_KEY \
///     --broadcast --verify --verifier blockscout \
///     --verifier-url https://hsk.blockscout.com/api/
///
/// Cost on HSK Chain at 2026-11-25 gas prices (0.0000005 HSK/gas): ~1.25 HSK = ~$0.10.
contract Deploy is Script {
    // HSK Chain mainnet, verified on chain 2026-11-25
    address constant WHSK = 0xB210D2120d57b758EE163cFfb43e73728c471Cf1; // Wrapped HSK, 18dp
    address constant USDT = 0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029; // Tether USD, 6dp
    address constant POOL = 0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC; // WHSK/USDT 0.30%
    uint24 constant POOL_FEE = 3000;

    function run() external returns (address) {
        address owner = vm.envAddress("VAULT_OWNER");
        address agent = vm.envAddress("VAULT_AGENT");
        uint256 pk = vm.envUint("PRIVATE_KEY");

        // Deliberately tiny limits. This pool clears ~$558/day: a vault that allows large
        // trades here is lying to its user about what the market can absorb.
        GuardrailVault.Policy memory policy = GuardrailVault.Policy({
            maxTradeUsd: 100e6, // $100 per trade
            dailyLossUsd: 5e6, // $5 daily stop
            cooldownSec: 60, // 1 minute between trades
            maxInventory: 2_000e18, // 2,000 WHSK ceiling
            minEdgeBps: 50, // demand 50bp of edge ABOVE cost
            maxSlippageBps: 200,
            maxImpactBps: 300 // refuse if one trade moves price >3%
        });

        vm.startBroadcast(pk);
        GuardrailVault vault = new GuardrailVault(
            owner, agent, WHSK, USDT, POOL, POOL_FEE, 3600, policy
        );
        vm.stopBroadcast();

        console.log("GuardrailVault deployed at:", address(vault));
        console.log("  owner :", owner);
        console.log("  agent :", agent);
        console.log("  pool  :", POOL);

        // prove the deployed instance can read the live market
        (uint256 spot, uint256 twap, uint256 costBps, uint256 impactBps, bool tradable, uint256 edge) =
            vault.marketReport(100e6);
        console.log("");
        console.log("LIVE MARKET REPORT (from the deployed contract)");
        console.log("  spot       :", spot);
        console.log("  twap 1h    :", twap);
        console.log("  round trip :", costBps, "bps");
        console.log("  impact     :", impactBps, "bps");
        console.log("  market edge:", edge, "bps");
        console.log("  tradable   :", tradable);

        return address(vault);
    }
}
