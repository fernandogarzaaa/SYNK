import React from 'react';
import { Play, ShieldCheck, Globe, AlertTriangle } from 'lucide-react';

export const WorldStateView = ({ world, ownership }) => {
  const activeTab = world?.active_tab || "default";
  const activeUrl = world?.tabs?.[activeTab]?.url || "N/A";
  const lockCount = ownership ? Object.keys(ownership).length : 0;
  return (
    <div className="p-4 bg-slate-900 text-slate-100 rounded-lg border border-slate-700">
      <div className="flex items-center gap-2 mb-4 text-blue-400">
        <Globe size={18} />
        <h3 className="font-bold uppercase text-xs tracking-wider">World State</h3>
      </div>

      <div className="space-y-2 text-sm">
        <div className="flex justify-between items-center p-2 bg-slate-800 rounded">
          <span className="text-slate-400">Active tab</span>
          <span className="font-mono text-xs truncate ml-4">{activeTab} (v{world?.version ?? "?"})</span>
        </div>
        <div className="flex justify-between items-center p-2 bg-slate-800 rounded">
          <span className="text-slate-400">Current URL</span>
          <span className="font-mono text-xs truncate ml-4">{activeUrl}</span>
        </div>

        <div className="flex justify-between items-center p-2 bg-slate-800 rounded">
          <span className="text-slate-400">Active locks</span>
          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700 text-slate-200">
            {lockCount}
          </span>
        </div>
      </div>
    </div>
  );
};

export const ActionPanel = ({ onAction }) => {
  return (
    <div className="p-4 bg-slate-900 text-slate-100 rounded-lg border border-slate-700 mt-4">
      <div className="flex items-center gap-2 mb-4 text-purple-400">
        <Play size={18} />
        <h3 className="font-bold uppercase text-xs tracking-wider">Execution</h3>
      </div>
      
      <div className="grid grid-cols-2 gap-2">
        <button
          onClick={() => onAction({ tool: 'snapshot' })}
          className="p-2 bg-slate-800 hover:bg-slate-700 rounded border border-slate-600 text-xs transition-colors"
        >
          Sync State
        </button>
        <button
          onClick={() => onAction({ tool: 'navigate', url: 'https://example.com' })}
          className="p-2 bg-slate-800 hover:bg-slate-700 rounded border border-slate-600 text-xs transition-colors"
        >
          Go example.com
        </button>
        <button
          onClick={() => onAction({ tool: 'click', args: 'delete account test' })}
          className="col-span-2 p-2 bg-red-900/30 hover:bg-red-900/50 rounded border border-red-900 text-xs transition-colors"
        >
          Test destructive (consent demo)
        </button>
      </div>
    </div>
  );
};

export const TruthLayerStatus = ({ result, onVerify }) => {
  if (!result) return (
    <div className="mt-4 p-4 border border-dashed border-slate-700 rounded-lg text-center text-xs text-slate-500 bg-slate-900/50">
      <div className="flex flex-col items-center gap-2">
        <ShieldCheck size={20} className="opacity-30" />
        <p>No active verification claim</p>
        <button 
          onClick={() => onVerify("c1")}
          className="px-3 py-1 bg-slate-800 hover:bg-slate-700 rounded text-slate-300 transition-colors"
        >
          Verify Sample Claim (c1)
        </button>
      </div>
    </div>
  );
  
  const colors = {
    VERIFIED: 'text-green-400 border-green-900 bg-green-900/20',
    FAILED: 'text-red-400 border-red-900 bg-red-900/20',
    CONFLICTING: 'text-orange-400 border-orange-900 bg-orange-900/20',
    UNVERIFIED: 'text-slate-400 border-slate-700 bg-slate-800/50',
  };

  return (
    <div className={`mt-4 p-3 border rounded-lg text-xs ${colors[result.result] || colors.UNVERIFIED}`}>
      <div className="flex items-center gap-2 font-bold mb-1">
        {result.result === 'VERIFIED' ? <ShieldCheck size={14} /> : <AlertTriangle size={14} />}
        {result.result}
      </div>
      <p className="opacity-80">{result.reason}</p>
      <div className="mt-2 text-[10px] font-mono opacity-60">
        Confidence: {(result.confidence * 100).toFixed(1)}%
      </div>
    </div>
  );
};

