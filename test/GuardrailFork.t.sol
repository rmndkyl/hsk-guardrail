// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {GuardrailVault} from "../src/GuardrailVault.sol";

/// @notice Fork tests against the real HSK Chain mainnet WHSK/USDT pool.
///         These prove the cost model and the edge gate against live pool state, not against
///         a mock we wrote to agree with ourselves.
contract GuardrailForkTest is Test {
    address constant POOL = 0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC; // WHSK/USDT 0.30%
    address constant WHSK = 0xB210D2120d57b758EE163cFfb43e73728c471Cf1;
    address constant USDT = 0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029;
    address constant ROUTER = 0x2c16f75b95Cf1390c328aB70e2CEE7f4b80bD8F3; // SwapRouter02
    address constant QUOTER = 0x1095A23A4812185344D9abd5143191D8a3A7E36f; // QuoterV2

    GuardrailVault vault;
    address owner = address(0xA11CE);
    address agent = address(0xB0B);

    uint256 constant MAX_TRADE = 100e6;
    uint256 constant MIN_EDGE = 50;

    function setUp() public {
        vm.createSelectFork(vm.envOr("HSK_RPC", string("https://mainnet.hsk.xyz")));
        GuardrailVault.Policy memory p = GuardrailVault.Policy({
            maxTradeUsd: MAX_TRADE,
            dailyLossUsd: 5e6,
            cooldownSec: 60,
            maxInventory: 2_000e18,
            minEdgeBps: MIN_EDGE,
            maxSlippageBps: 200,
            maxImpactBps: 300
        });
        vault = new GuardrailVault(owner, agent, WHSK, USDT, POOL, 3000, 3600, p);
    }

    // ------------------------------------------------------------ price + oracle

    function test_spotPrice_isCorrectScale() public view {
        uint256 spot = vault.spotPriceE18();
        console.log("spot (USDT/WHSK, 1e18):", spot);
        // Nov 2026: HSK traded around $0.08. Wide band so the test does not rot.
        assertGt(spot, 0.01e18, "spot must be a real sub-dollar price, not zero");
        assertLt(spot, 1e18, "spot must be sub-dollar for HSK");
    }

    function test_twap_isAvailableFromRingBuffer() public view {
        uint256 twap = vault._twapAt(3600);
        assertGt(twap, 0, "TWAP must be computable from the observation buffer");
        console.log("twap 1h:", twap);
    }

    // ------------------------------------------------------------ cost model vs truth

    function test_costModel_roundTripIsNearTwoLegsOfFee() public view {
        (uint256 rt10,) = vault.roundTripCostBps(10e6);
        (uint256 rt100,) = vault.roundTripCostBps(100e6);
        console.log("round-trip $10  bps:", rt10);
        console.log("round-trip $100 bps:", rt100);
        // On a v3 pool the impact of the buy is handed back on the sell, so round-trip cost
        // collapses to ~2x the 30bp fee regardless of size.
        assertGe(rt10, 40, "round trip must cost something real");
        assertLe(rt10, 90, "round trip should sit near 2x the pool fee");
        assertApproxEqAbs(rt10, rt100, 10, "round trip is size invariant on v3");
    }

    /// @dev Single-leg impact is what actually scales, which is why the vault gates on it.
    function test_singleLegPriceWorsensWithSize() public {
        uint256 px10 = _execPrice(10e6);
        uint256 px500 = _execPrice(500e6);
        console.log("exec price $10 :", px10);
        console.log("exec price $500:", px500);
        assertGt(px500, px10, "bigger buy must get a worse execution price");
    }

    // ------------------------------------------------------------ THE core gate

    /// @dev The differentiating behaviour: this pool's spot sits essentially on its own TWAP,
    ///      so the market offers almost no edge and the vault refuses -- even when the agent
    ///      claims a large edge and the size is well within the cap.
    function test_refusesBecauseMarketHasNoEdge() public view {
        (uint256 marketEdge,, uint256 widestWindow) = vault.availableEdgeBps();
        console.log("market edge bps:", marketEdge);
        console.log("widest window s:", widestWindow);

        GuardrailVault.Decision memory d = vault.evaluate(10e6, 5_000);
        console.log("refusal reason:", d.reason);
        console.log("cost at $10   :", d.roundTripCostBps);

        if (marketEdge < MIN_EDGE + d.roundTripCostBps) {
            assertFalse(d.allowed, "must refuse when the market offers no edge");
            assertEq(d.reason, uint16(GuardrailVault.Reason.MarketHasNoEdge));
        } else {
            assertEq(d.reason, uint16(GuardrailVault.Reason.ClaimExceedsMarket));
        }
    }

    /// @dev A model that invents an edge bigger than the market is holding gets refused
    ///      on numbers the vault measured itself.
    function test_refusesClaimThatExceedsMarket() public view {
        (uint256 marketEdge,,) = vault.availableEdgeBps();
        (uint256 costBps,) = vault.roundTripCostBps(10e6);
        if (marketEdge < costBps + MIN_EDGE) {
            // nothing to claim against today; the market-has-no-edge test covers this state
            return;
        }
        GuardrailVault.Decision memory d = vault.evaluate(10e6, marketEdge + 1);
        assertFalse(d.allowed);
        assertEq(d.reason, uint16(GuardrailVault.Reason.ClaimExceedsMarket));
    }

    function test_allowsOnlyWhenClaimFitsInsideMarketEdge() public view {
        (uint256 marketEdge,,) = vault.availableEdgeBps();
        (uint256 costBps,) = vault.roundTripCostBps(10e6);
        if (marketEdge < costBps + MIN_EDGE) {
            // correct behaviour today: nothing is tradable
            GuardrailVault.Decision memory d = vault.evaluate(10e6, marketEdge);
            assertFalse(d.allowed);
            return;
        }
        GuardrailVault.Decision memory d = vault.evaluate(10e6, marketEdge);
        assertTrue(d.allowed, "a claim equal to the measured market edge is allowed");
        assertEq(d.reason, uint16(GuardrailVault.Reason.Ok));
    }

    // ------------------------------------------------------------ other refusals

    function test_refusesOversizedTradeOnCap() public view {
        GuardrailVault.Decision memory d = vault.evaluate(MAX_TRADE + 1, 10_000);
        assertFalse(d.allowed);
        assertEq(d.reason, uint16(GuardrailVault.Reason.CapExceeded));
    }

    function test_refusesWhenPaused() public {
        vm.prank(owner);
        vault.pause();
        GuardrailVault.Decision memory d = vault.evaluate(10e6, 10_000);
        assertFalse(d.allowed);
        assertEq(d.reason, uint16(GuardrailVault.Reason.Paused));
    }

    function test_refusesWhenDailyLossBudgetSpent() public {
        vm.prank(agent);
        vault.reportLoss(5e6);
        GuardrailVault.Decision memory d = vault.evaluate(10e6, 10_000);
        assertFalse(d.allowed);
        assertEq(d.reason, uint16(GuardrailVault.Reason.DailyLossSpent));
    }

    // ------------------------------------------------------------ attestation of refusals

    function test_attestStoresRefusalOnChain() public {
        uint256 before = vault.decisionCount();
        vm.prank(agent);
        uint256 id = vault.attest(uint256(keccak256("decision: hold, market edge too thin")), 10e6, 5);
        assertEq(id, before);
        assertEq(vault.decisionCount(), before + 1);

        GuardrailVault.Decision memory d = vault.decisionAt(id);
        assertFalse(d.allowed);
        assertGt(d.roundTripCostBps, 0, "the vault records what it measured");
        assertGt(d.spotPriceE18, 0, "and the price it saw");
        console.log("attested refusal reason:", d.reason);
        console.log("attested market edge bps:", d.marketEdgeBps);
    }

    // ------------------------------------------------------------ market report

    function test_marketReport_tellsTheTruth() public view {
        (uint256 spot, uint256 twap, uint256 costBps, uint256 impactBps, bool tradable, uint256 edge) =
            vault.marketReport(100e6);
        console.log("spot       :", spot);
        console.log("twap       :", twap);
        console.log("cost bps   :", costBps);
        console.log("impact bps :", impactBps);
        console.log("market edge:", edge);
        console.log("tradable   :", tradable);
        assertGt(spot, 0);
        assertGt(twap, 0);
        assertGe(costBps, 40);
    }

    // ------------------------------------------------------------ owner safety

    function test_ownerCannotLoosenPolicy() public {
        GuardrailVault.Policy memory loose = vault.getPolicy();
        loose.maxTradeUsd = 10_000e6;
        vm.prank(owner);
        vm.expectRevert(GuardrailVault.PolicyOnlyTightens.selector);
        vault.tighten(loose);
    }

    function test_ownerCanTightenPolicy() public {
        GuardrailVault.Policy memory tight = vault.getPolicy();
        tight.maxTradeUsd = 50e6;
        tight.minEdgeBps = 100;
        vm.prank(owner);
        vault.tighten(tight);
        GuardrailVault.Policy memory now_ = vault.getPolicy();
        assertEq(now_.maxTradeUsd, 50e6);
        assertEq(now_.minEdgeBps, 100);
    }

    function test_strangerCannotTrade() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert(GuardrailVault.NotAgent.selector);
        vault.trade(10e6, 10_000, 0, ROUTER);
    }

    // ------------------------------------------------------------ helpers

    /// @dev QuoterV2 is *not* a view contract: it prices a swap by executing it against the
    ///      pool and reverting with the result. A staticcall therefore fails with a
    ///      state-modification violation instead of returning a quote, so this must be a
    ///      plain call. (`eth_call` from Python works because it permits the inner revert.)
    function _quoteSingle(address tin, address tout, uint256 amt, uint24 fee)
        internal
        returns (uint256)
    {
        (bool ok, bytes memory data) = QUOTER.call(
            abi.encodeWithSelector(0xc6a5026a, tin, tout, amt, fee, uint160(0))
        );
        if (!ok || data.length < 32) return 0;
        return abi.decode(data, (uint256));
    }

    /// @dev execution price (USDT per WHSK, 1e18) for buying `usd` worth
    function _execPrice(uint256 usd) internal returns (uint256) {
        uint256 out = _quoteSingle(USDT, WHSK, usd, 3000);
        require(out > 0, "quote failed");
        return (usd * 1e18 * 1e18) / out;
    }
}
