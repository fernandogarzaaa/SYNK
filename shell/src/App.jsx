import React, { useState, useEffect } from 'react';
import { invoke } from '@tauri-apps/api/tauri';
import { WorldStateView, ActionPanel, TruthLayerStatus } from './components/RuntimeUI';
import { EventLog } from './components/EventLog';
import { OwnershipMonitor } from './components/OwnershipMonitor';

function App() {
  const [state, setState] = useState({ world: {}, ownership: {}, events: [] });
  const [verification, setVerification] = useState(null);
  const [loading, setLoading] = useState(false);

  const refreshWorld = async () => {
    try {
      const data = await invoke('get_world_state');
      setState(data);
    } catch (e) {
      console.error("Failed to fetch world state", e);
    }
  };


  const handleAction = async (action) => {
    setLoading(true);
    try {
      // Server /act expects {actions:[...], page_url, tab_id, user_consented}
      const body = {
        actions: [action],
        page_url: state.world?.tabs?.[state.world?.active_tab]?.url || "",
        tab_id: state.world?.active_tab || "default",
        user_consented: false,
      };
      await invoke('submit_action', { body });
      await refreshWorld();
    } catch (e) {
      console.error("Action failed", e);
    } finally {
      setLoading(false);
    }
  };

  const handleVerify = async (claimId) => {
    setLoading(true);
    try {
      const res = await invoke('verify_claim', { claimId });
      setVerification(res);
    } catch (e) {
      console.error("Verification failed", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {

    refreshWorld();
    const timer = setInterval(refreshWorld, 5000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="min-h-screen bg-black text-slate-200 p-6 font-sans">
      <div className="max-w-md mx-auto space-y-6">
        <header className="flex justify-between items-center mb-8">
          <div>
            <h1 className="text-2xl font-black tracking-tighter text-white">SYNK <span className="text-blue-500">SHELL</span></h1>
            <p className="text-xs text-slate-500 font-mono">Runtime: CDP-Enabled</p>
          </div>
          <div className={`h-2 w-2 rounded-full ${loading ? 'bg-yellow-500 animate-pulse' : 'bg-green-500'}`} />
        </header>

        <WorldStateView world={state.world} ownership={state.ownership} />
        
        <OwnershipMonitor ownership={state.ownership} />
        
        <ActionPanel onAction={handleAction} />
        
        <EventLog events={state.events} />

        <TruthLayerStatus result={verification} onVerify={handleVerify} />
        
        <footer className="pt-12 text-center">


          <p className="text-[10px] text-slate-600 uppercase tracking-widest font-bold">
            Truth Layer Independent Verification Active
          </p>
        </footer>
      </div>
    </div>
  );
}

export default App;
