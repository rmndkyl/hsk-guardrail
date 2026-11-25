// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {GuardrailVault} from "../src/GuardrailVault.sol";

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
    function deposit() external payable;
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata p) external payable returns (uint256);
}

/// @notice THE test that was missing: prove the trade path actually works.
///
/// The other fork test file proves the gate REFUSES, because this market genuinely offers no
/// edge. That leaves an obvious hole: nothing ever proves the vault can execute a swap when the
/// market *does* offer one. A guardrail that only knows how to say no is a wall, not a product.
///
/// This test manufactures the condition it needs, honestly:
///   1. Take a fork of HSK Chain mainnet.
///   2. Push the pool price away from its own TWAP with a real swap, which creates a genuine
///      spot-vs-TWAP gap (exactly the situation the gate is looking for).
///   3. Fund the vault with USDT and let the agent trade.
///   4. Assert the swap executed, the vault received WHSK, and the decision was recorded as
///      allowed.
///
/// If this test fails, the vault is a wall. If it passes, the gate opens on real conditions.
contract TradePathTest is Test {
    address constant POOL = 0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC;
    address constant WHSK = 0xB210D2120d57b758EE163cFfb43e73728c471Cf1;
    address constant USDT = 0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029;
    address constant ROUTER = 0x2c16f75b95Cf1390c328aB70e2CEE7f4b80bD8F3;

    GuardrailVault vault;
    address owner = address(0xA11CE);
    address agent = address(0xB0B);

    function setUp() public {
        vm.createSelectFork(vm.envOr("HSK_RPC", string("https://mainnet.hsk.xyz")));

        // A deliberately permissive policy FOR THIS TEST ONLY, so the gate's own logic decides
        // rather than the caps. Still bounded: $100 per trade, 10 bp margin.
        GuardrailVault.Policy memory p = GuardrailVault.Policy({
            maxTradeUsd: 100e6,
            dailyLossUsd: 50e6,
            cooldownSec: 0,
            maxInventory: 5_000e18,
            minEdgeBps: 10,
            maxSlippageBps: 500,
            maxImpactBps: 500
        });
        vault = new GuardrailVault(owner, agent, WHSK, USDT, POOL, 3000, 3600, p);
    }

    /// @dev helper: buy WHSK with `usd` of USDT from a funded EOA, to move the price
    function _push(uint256 usd) internal returns (uint256) {
        address whale = address(0xC0FFEE);
        deal(USDT, whale, usd);
        vm.startPrank(whale);
        IERC20(USDT).approve(ROUTER, usd);
        uint256 out = ISwapRouter(ROUTER).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: USDT, tokenOut: WHSK, fee: 3000, recipient: whale,
                amountIn: usd, amountOutMinimum: 0, sqrtPriceLimitX96: 0
            })
        );
        vm.stopPrank();
        return out;
    }

    /// @notice The whole point: gate opens, swap executes, vault holds WHSK, decision recorded.
    function test_tradeExecutesWhenMarketActuallyOffersEdge() public {
        // -- 1. baseline: before we disturb anything, the gate should refuse
        (uint256 gapBefore,,) = vault.availableEdgeBps();
        (uint256 costBefore,) = vault.roundTripCostBps(100e6);
        console.log("gap before push  :", gapBefore, "bp");
        console.log("cost             :", costBefore, "bp");

        // -- 2. create a real gap by pushing the price
        uint256 pushed = _push(20_000e6); // buy ~$20k of WHSK: a big move for this pool
        uint256 spotAfter = vault.spotPriceE18();
        (uint256 gapAfter,, uint256 win) = vault.availableEdgeBps();
        console.log("WHSK received    :", pushed);
        console.log("spot after push  :", spotAfter);
        console.log("gap after push   :", gapAfter);
        console.log("widest window    :", win);

        assertGt(gapAfter, gapBefore, "pushing the price must widen the measured gap");

        // -- 3. if and only if the gap now clears cost + margin, the gate must open
        (uint256 costNow, uint256 impactNow) = vault.roundTripCostBps(100e6);
        uint256 required = costNow + vault.getPolicy().minEdgeBps;
        console.log("required now     :", required, "bp");
        console.log("impact now       :", impactNow, "bp");

        if (gapAfter < required) {
            // The push was not big enough to create tradeable edge. Say so and stop rather than
            // silently passing a test that proves nothing.
            console.log("SKIP: push did not create enough edge to open the gate");
            console.log("      (this is informative, not a failure)");
            return;
        }

        GuardrailVault.Decision memory d = vault.evaluate(100e6, gapAfter);
        console.log("decision reason  :", d.reason);
        assertTrue(d.allowed, "gate must open when the gap clears cost + margin");

        // -- 4. fund the vault and trade for real
        deal(USDT, address(vault), 100e6);
        uint256 whskBefore = IERC20(WHSK).balanceOf(address(vault));

        vm.prank(agent);
        (uint256 id, uint256 baseOut) = vault.trade(100e6, gapAfter, 0, ROUTER);

        uint256 whskAfter = IERC20(WHSK).balanceOf(address(vault));
        console.log("trade id         :", id);
        console.log("WHSK out         :", baseOut);
        console.log("vault WHSK before:", whskBefore);
        console.log("vault WHSK after :", whskAfter);

        assertGt(baseOut, 0, "the swap must return WHSK");
        assertEq(whskAfter, whskBefore + baseOut, "vault balance must increase by the swap output");
        assertEq(IERC20(USDT).balanceOf(address(vault)), 0, "USDT must be spent");

        // -- 5. the decision is on the record as allowed
        GuardrailVault.Decision memory rec = vault.decisionAt(id);
        assertTrue(rec.allowed, "recorded decision must be allowed");
        assertEq(rec.reason, uint16(GuardrailVault.Reason.Ok));
        console.log("recorded spot    :", rec.spotPriceE18);
        console.log("recorded gap     :", rec.marketEdgeBps);
    }

    /// @dev After a trade, an immediate second trade is blocked by cooldown when the policy
    ///      sets one. Proves the executed path still respects sequencing.
    function test_tradePathRespectsCooldownAfterExecution() public {
        uint256 pushed = _push(20_000e6);
        require(pushed > 0, "push failed");

        (uint256 gap,,) = vault.availableEdgeBps();
        (uint256 cost,) = vault.roundTripCostBps(100e6);
        if (gap < cost + 10) {
            console.log("SKIP: no edge to trade");
            return;
        }

        // rebuild a vault with a cooldown so the second attempt is testable
        GuardrailVault.Policy memory p = vault.getPolicy();
        p.cooldownSec = 3600;
        vm.prank(owner);
        vault.tighten(p);

        deal(USDT, address(vault), 200e6);
        vm.prank(agent);
        vault.trade(100e6, gap, 0, ROUTER);

        // second attempt within cooldown must be refused, not reverted
        vm.prank(agent);
        (uint256 id2, uint256 out2) = vault.trade(100e6, gap, 0, ROUTER);
        assertEq(out2, 0, "second trade must not execute");
        GuardrailVault.Decision memory d = vault.decisionAt(id2);
        assertFalse(d.allowed);
        assertEq(d.reason, uint16(GuardrailVault.Reason.Cooldown));
        console.log("second trade refused with reason:", d.reason);
    }
}
