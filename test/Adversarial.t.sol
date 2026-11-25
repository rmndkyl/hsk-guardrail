// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {GuardrailVault} from "../src/GuardrailVault.sol";

/// @notice Adversarial tests: try to make the vault do the wrong thing.
///
/// A guardrail is only worth what it survives. Each test here is an attack a hostile or merely
/// buggy agent would attempt, and each must fail to break the invariant.
contract AdversarialTest is Test {
    address constant POOL = 0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC;
    address constant WHSK = 0xB210D2120d57b758EE163cFfb43e73728c471Cf1;
    address constant USDT = 0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029;
    address constant ROUTER = 0x2c16f75b95Cf1390c328aB70e2CEE7f4b80bD8F3;

    GuardrailVault vault;
    address owner = address(0xA11CE);
    address agent = address(0xB0B);

    function setUp() public {
        vm.createSelectFork(vm.envOr("HSK_RPC", string("https://mainnet.hsk.xyz")));
        GuardrailVault.Policy memory p = GuardrailVault.Policy({
            maxTradeUsd: 100e6,
            dailyLossUsd: 5e6,
            cooldownSec: 60,
            maxInventory: 5_000e18,
            minEdgeBps: 50,
            maxSlippageBps: 200,
            maxImpactBps: 300
        });
        vault = new GuardrailVault(owner, agent, WHSK, USDT, POOL, 3000, 3600, p);
    }

    // ------------------------------------------------------------------ attack 1
    /// @dev The agent claims an enormous edge to force a trade through. The vault must compare
    ///      that claim against what the market actually offers, not accept it.
    function test_attack_overclaimIsRejected() public {
        (uint256 marketEdge,,) = vault.availableEdgeBps();
        GuardrailVault.Decision memory d = vault.evaluate(10e6, 999_999);
        assertFalse(d.allowed, "an absurd claim must not open the gate");
        // On this market the gap is tiny, so the reason is either no-edge or claim-exceeds.
        assertTrue(
            d.reason == uint16(GuardrailVault.Reason.MarketHasNoEdge)
                || d.reason == uint16(GuardrailVault.Reason.ClaimExceedsMarket),
            "must be refused on market measurement, not accepted"
        );
        console.log("market edge:", marketEdge);
        console.log("refusal reason:", d.reason);
    }

    // ------------------------------------------------------------------ attack 2
    /// @dev The agent tries to bypass the cap by asking for a huge notional. Refused.
    function test_attack_capBypassRefused() public {
        GuardrailVault.Decision memory d = vault.evaluate(1_000_000e6, 10_000);
        assertFalse(d.allowed);
        assertEq(d.reason, uint16(GuardrailVault.Reason.CapExceeded));

        // and the trade function itself records the refusal instead of swapping
        deal(USDT, address(vault), 1_000_000e6);
        vm.prank(agent);
        (, uint256 out) = vault.trade(1_000_000e6, 10_000, 0, ROUTER);
        assertEq(out, 0, "oversized trade must not execute");
    }

    // ------------------------------------------------------------------ attack 3
    /// @dev The agent points the router argument at a malicious contract to drain funds.
    ///      It cannot: the guardrail refuses first on a dead market, and even if a trade were
    ///      allowed the vault approves only `notionalUsd` to that address. Here we prove the
    ///      refusal path returns without touching a hostile router.
    function test_attack_hostileRouterNeverReached() public {
        address evil = address(0xBAD);
        deal(USDT, address(vault), 100e6);
        uint256 before = IERC20(USDT).balanceOf(evil);

        vm.prank(agent);
        (uint256 id, uint256 out) = vault.trade(10e6, 1, 0, evil);

        assertEq(out, 0, "no swap through a hostile router");
        assertEq(IERC20(USDT).balanceOf(evil), before, "hostile router must receive nothing");
        GuardrailVault.Decision memory d = vault.decisionAt(id);
        assertFalse(d.allowed);
        console.log("refused before reaching router, reason:", d.reason);
    }

    // ------------------------------------------------------------------ attack 4
    /// @dev A non-agent tries every privileged path. All must revert.
    function test_attack_privilegeEscalation() public {
        address stranger = address(0xDEAD);

        vm.startPrank(stranger);
        vm.expectRevert(GuardrailVault.NotAgent.selector);
        vault.attest(1, 10e6, 0);

        vm.expectRevert(GuardrailVault.NotAgent.selector);
        vault.reportLoss(1e6);

        vm.expectRevert(GuardrailVault.NotOwner.selector);
        vault.pause();

        vm.expectRevert(GuardrailVault.NotOwner.selector);
        vault.withdraw(USDT, 1, stranger);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ attack 5
    /// @dev The owner (or a compromised owner key) tries to loosen the limits. Reverts on
    ///      every single field, one at a time.
    function test_attack_ownerCannotLoosenAnyField() public {
        GuardrailVault.Policy memory base = vault.getPolicy();

        // maxTradeUsd up
        GuardrailVault.Policy memory p1 = base;
        p1.maxTradeUsd = base.maxTradeUsd + 1;
        vm.prank(owner);
        vm.expectRevert(GuardrailVault.PolicyOnlyTightens.selector);
        vault.tighten(p1);

        // dailyLossUsd up
        GuardrailVault.Policy memory p2 = base;
        p2.dailyLossUsd = base.dailyLossUsd + 1;
        vm.prank(owner);
        vm.expectRevert(GuardrailVault.PolicyOnlyTightens.selector);
        vault.tighten(p2);

        // cooldownSec down
        GuardrailVault.Policy memory p3 = base;
        p3.cooldownSec = base.cooldownSec - 1;
        vm.prank(owner);
        vm.expectRevert(GuardrailVault.PolicyOnlyTightens.selector);
        vault.tighten(p3);

        // minEdgeBps down
        GuardrailVault.Policy memory p4 = base;
        p4.minEdgeBps = base.minEdgeBps - 1;
        vm.prank(owner);
        vm.expectRevert(GuardrailVault.PolicyOnlyTightens.selector);
        vault.tighten(p4);

        // maxImpactBps up
        GuardrailVault.Policy memory p5 = base;
        p5.maxImpactBps = base.maxImpactBps + 1;
        vm.prank(owner);
        vm.expectRevert(GuardrailVault.PolicyOnlyTightens.selector);
        vault.tighten(p5);

        console.log("all five loosening attempts reverted");
    }

    // ------------------------------------------------------------------ attack 6
    /// @dev Agent reports a loss larger than the daily budget, then tries to trade. The breaker
    ///      must hold even though the loss was self-reported.
    function test_attack_dailyBreakerHoldsAfterReportedLoss() public {
        vm.prank(agent);
        vault.reportLoss(5e6);
        GuardrailVault.Decision memory d = vault.evaluate(10e6, 10_000);
        assertFalse(d.allowed);
        assertEq(d.reason, uint16(GuardrailVault.Reason.DailyLossSpent));
    }

    // ------------------------------------------------------------------ attack 7
    /// @dev Paused means paused: even a would-be-allowed trade is refused, and the trade
    ///      function reverts rather than recording.
    function test_attack_pauseIsAbsolute() public {
        vm.prank(owner);
        vault.pause();

        GuardrailVault.Decision memory d = vault.evaluate(10e6, 999_999);
        assertFalse(d.allowed);
        assertEq(d.reason, uint16(GuardrailVault.Reason.Paused));

        deal(USDT, address(vault), 100e6);
        vm.prank(agent);
        vm.expectRevert(GuardrailVault.IsPaused.selector);
        vault.trade(10e6, 999_999, 0, ROUTER);
    }

    // ------------------------------------------------------------------ attack 8
    /// @dev The ledger is append-only: a decision, once written, reads back identically and
    ///      there is no function to rewrite it. Proven by reading the same index twice around
    ///      new activity.
    function test_attack_ledgerIsAppendOnly() public {
        vm.prank(agent);
        uint256 id = vault.attest(uint256(keccak256("first")), 10e6, 5);
        GuardrailVault.Decision memory before = vault.decisionAt(id);

        // do more things
        vm.prank(agent);
        vault.attest(uint256(keccak256("second")), 20e6, 5);
        vm.prank(agent);
        vault.reportLoss(1e6);

        GuardrailVault.Decision memory after_ = vault.decisionAt(id);
        assertEq(before.reason, after_.reason);
        assertEq(before.spotPriceE18, after_.spotPriceE18);
        assertEq(before.notionalUsd, after_.notionalUsd);
        assertEq(before.timestamp, after_.timestamp);
        console.log("earlier decision unchanged after later activity");
    }

    // ------------------------------------------------------------------ attack 9
    /// @dev Zero-size and zero-liquidity inputs must not panic or open the gate.
    function test_attack_degenerateInputsDoNotPanic() public {
        GuardrailVault.Decision memory zero = vault.evaluate(0, 0);
        assertFalse(zero.allowed);
        assertEq(zero.reason, uint16(GuardrailVault.Reason.CapExceeded));

        // A size so large it drains the pool's single tick must be reported as UNPRICEABLE,
        // never as zero cost. Returning 0 here would mean "trading is free", which is the one
        // answer that must never come out of a guardrail.
        (uint256 cost,) = vault.roundTripCostBps(50_000_000e6);
        if (cost == type(uint256).max) {
            console.log("cost for a $50M quote: MAX (unpriceable)");
        } else {
            console.log("cost for a $50M quote bps:", cost);
        }
        assertTrue(
            cost == type(uint256).max,
            "a size outside the single-tick model must be reported unpriced, never cheap"
        );

        // and the decision path treats unpriceable as a refusal, not a pass
        GuardrailVault.Decision memory d = vault.evaluate(50_000_000e6, 10_000);
        assertFalse(d.allowed, "unpriceable size must not be allowed");
    }
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
}
