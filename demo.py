import os
import time
import httpx
from sdk import Trace

# Configuration matching docker-compose.yml
AGENTGATE_URL = "http://localhost:8000"
FLAKY_API_URL = "http://localhost:9000/mcp"
DEMO_KEY = "ag_live_demo_key_123"

def run_advanced_demo():
    print("\n" + "="*60)
    print("🚀 AGENTGATE ADVANCED DEMO: THE AGENTIC CONTROL PLANE")
    print("="*60)
    
    trace = Trace(api_key=DEMO_KEY, base_url=AGENTGATE_URL)

    # --- ACT 1: INVISIBLE RELIABILITY ---
    print("\n[ACT 1] The Invisible Safety Net")
    print("Scenario: Calling a 'Flaky' tool that fails 4 times before succeeding.")
    print("Agent Code: simple, linear, no try/except logic.")
    
    start = time.time()
    try:
        res = trace.call(tool="fetch.url", params={"url": "https://agentgate.dev"}, agent_id="reliability_pro")
        print(f"✅ SUCCESS: Recieved result after AgentGate silently handled the retries.")
        print(f"   Time taken: {time.time() - start:.2f}s (includes 4 backend failures)")
    except Exception as e:
        print(f"❌ FAILED: {e}")

    # --- ACT 2: GOVERNANCE & HITL ---
    print("\n[ACT 2] The Governance Guardrail")
    print("Scenario: Agent attempts a 'Risky' action (GitHub Issue creation).")
    
    try:
        risky_call = trace.call(
            tool="github.create_issue",
            params={"repo": "acme/corp", "title": "Critical Bug", "body": "Found by agent"},
            agent_id="governance_pro"
        )
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return

    if risky_call.get("status") == "pending_approval":
        appr_id = risky_call["approval_id"]
        print(f"⚠️  PAUSED: Action intercepted. Risk level: {risky_call['approval']['risk_level']}")
        print(f"👉 REVIEW HERE: {risky_call['approval']['dashboard_url']}")
        
        # --- ACT 3: THE \"WOW\" MOMENT (STATE DRIFT) ---
        print("\n[ACT 3] The 'Wait, What Changed?' (State Drift)")
        print("Scenario: A human is reviewing the dashboard. Suddenly, the environment becomes UNSTABLE.")
        
        # Simulate mid-approval environment change
        httpx.post(FLAKY_API_URL, json={"method": "demo/toggle_drift", "params": {}})
        print("🔥 SYSTEM SIMULATION: Environment status changed to 'UNSTABLE' mid-approval.")
        
        print("\nSimulating Human clicking 'APPROVE' on the dashboard...")
        time.sleep(3)
        
        try:
            # Human clicks approve, but AgentGate re-validates the 'stable' condition
            final_res = trace.approve(appr_id, note="I think it's safe now!")
            
            if final_res.get("status") == "requeued":
                print("🛡️  AGENTGATE BLOCKED EXECUTION!")
                print(f"   Reason: {final_res['requeue_reason']}")
                
                drift = final_res.get("decision", {}).get("drift", {})
                for check in drift.get("checks", []):
                    if check.get("changed") or not check.get("condition_passed"):
                        print(f"   👉 Field '{check['path']}' drifted: {check['approval_value']} -> {check['current_value']}")

                print("   Explanation: The human approved, but AgentGate detected that the system")
                print("   is no longer 'stable'. It prevented a stale/dangerous action.")
            else:
                print(f"   Status: {final_res.get('status')}")
        except Exception as e:
            print(f"❌ Error during approval: {e}")

    print("\n" + "="*60)
    print("🎯 SUMMARY FOR THE STARTUP DEV:")
    print("1. Reliability is a commodity (Retries are handled).")
    print("2. Human-in-the-loop is native.")
    print("3. State Revalidation (Drift Detection) stops agents from acting on stale data.")
    print("="*60 + "\n")

if __name__ == "__main__":
    run_advanced_demo()
