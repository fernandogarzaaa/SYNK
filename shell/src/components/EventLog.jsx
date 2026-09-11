import React from 'react';

export const EventLog = ({ events }) => {
  return (
    <div className="p-4 bg-slate-900 text-slate-100 rounded-lg border border-slate-700 mt-4">
      <div className="flex items-center gap-2 mb-4 text-emerald-400">
        <div className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
        <h3 className="font-bold uppercase text-xs tracking-wider">Runtime Events</h3>
      </div>
      
      <div className="space-y-2 max-h-64 overflow-y-auto pr-2 custom-scrollbar">
        {events && events.length > 0 ? (
          events.map((ev, i) => (
            <div key={i} className="p-2 bg-slate-800/50 border-l-2 border-slate-600 text-[11px] font-mono flex justify-between items-start gap-4">
              <span className="text-emerald-500 shrink-0">{ev.type}</span>
              <span className="text-slate-400 truncate">{JSON.stringify(ev.data)}</span>
            </div>
          ))
        ) : (
          <p className="text-center text-slate-600 text-xs py-4">No events recorded</p>
        )}
      </div>
    </div>
  );
};
