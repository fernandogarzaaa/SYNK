import React from 'react';
import { Lock, User, Bot } from 'lucide-react';

export const OwnershipMonitor = ({ ownership }) => {
  if (!ownership || Object.keys(ownership).length === 0) {
    return (
      <div className="p-4 bg-slate-900 text-slate-500 rounded-lg border border-slate-700 mt-4 text-center text-xs italic">
        No active ownership locks
      </div>
    );
  }

  // Assuming ownership is a map of target -> owner ('human' | 'agent')
  const entries = Object.entries(ownership);

  return (
    <div className="p-4 bg-slate-900 text-slate-100 rounded-lg border border-slate-700 mt-4">
      <div className="flex items-center gap-2 mb-4 text-amber-400">
        <Lock size={18} />
        <h3 className="font-bold uppercase text-xs tracking-wider">Ownership Graph</h3>
      </div>
      
      <div className="space-y-2">
        {entries.map(([target, owner]) => (
          <div key={target} className="flex justify-between items-center p-2 bg-slate-800 rounded text-xs">
            <span className="font-mono text-slate-400 truncate mr-4">{target}</span>
            <div className="flex items-center gap-2">
              {owner === 'human' ? (
                <>
                  <User size={12} className="text-blue-400" />
                  <span className="text-blue-400 font-bold uppercase text-[10px]">Human</span>
                </>
              ) : (
                <>
                  <Bot size={12} className="text-purple-400" />
                  <span className="text-purple-400 font-bold uppercase text-[10px]">Agent</span>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
