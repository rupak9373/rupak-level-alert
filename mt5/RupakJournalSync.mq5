#property strict
#property description "Rupak MT5 -> Web Trading Journal Sync"

input string BridgeURL = "https://rupak-broker-bridge.onrender.com";
input string SyncKey = "PASTE_JOURNAL_SYNC_KEY_HERE";
input int HistoryDays = 90;
input int SyncEverySeconds = 30;
input int MaxDealsPerSync = 200;

string JsonEscape(string s)
{
   StringReplace(s,"\\","\\\\");
   StringReplace(s,"\"","\\\"");
   StringReplace(s,"\r"," ");
   StringReplace(s,"\n"," ");
   return s;
}

string Num(double v,int digits=8)
{
   return DoubleToString(v,digits);
}

bool FindEntryForPosition(long position_id,double &entry,double &sl,double &tp,string &side)
{
   entry=0; sl=0; tp=0; side="Buy";
   int total=HistoryDealsTotal();
   datetime earliest=0;
   ulong entry_order=0;
   for(int i=0;i<total;i++)
   {
      ulong d=HistoryDealGetTicket(i);
      if(d==0) continue;
      if((long)HistoryDealGetInteger(d,DEAL_POSITION_ID)!=position_id) continue;
      long en=HistoryDealGetInteger(d,DEAL_ENTRY);
      if(en!=DEAL_ENTRY_IN && en!=DEAL_ENTRY_INOUT) continue;
      datetime tm=(datetime)HistoryDealGetInteger(d,DEAL_TIME);
      if(earliest==0 || tm<earliest)
      {
         earliest=tm;
         entry=HistoryDealGetDouble(d,DEAL_PRICE);
         long type=HistoryDealGetInteger(d,DEAL_TYPE);
         side=(type==DEAL_TYPE_SELL?"Sell":"Buy");
         entry_order=(ulong)HistoryDealGetInteger(d,DEAL_ORDER);
      }
   }
   if(entry_order>0 && HistoryOrderSelect(entry_order))
   {
      sl=HistoryOrderGetDouble(entry_order,ORDER_SL);
      tp=HistoryOrderGetDouble(entry_order,ORDER_TP);
   }
   return entry>0;
}

string MakeTradeJson(ulong deal)
{
   long entry_type=HistoryDealGetInteger(deal,DEAL_ENTRY);
   if(entry_type!=DEAL_ENTRY_OUT && entry_type!=DEAL_ENTRY_OUT_BY) return "";

   long position_id=(long)HistoryDealGetInteger(deal,DEAL_POSITION_ID);
   string symbol=HistoryDealGetString(deal,DEAL_SYMBOL);
   if(symbol=="") return "";

   double entry=0,sl=0,tp=0;
   string side="Buy";
   FindEntryForPosition(position_id,entry,sl,tp,side);

   double exit_price=HistoryDealGetDouble(deal,DEAL_PRICE);
   double volume=HistoryDealGetDouble(deal,DEAL_VOLUME);
   double profit=HistoryDealGetDouble(deal,DEAL_PROFIT);
   double swap=HistoryDealGetDouble(deal,DEAL_SWAP);
   double commission=HistoryDealGetDouble(deal,DEAL_COMMISSION);
   double fee=HistoryDealGetDouble(deal,DEAL_FEE);
   double pnl=profit+swap+commission+fee;
   datetime close_time=(datetime)HistoryDealGetInteger(deal,DEAL_TIME);
   string comment=HistoryDealGetString(deal,DEAL_COMMENT);
   long login=(long)AccountInfoInteger(ACCOUNT_LOGIN);

   double r=0;
   if(entry>0 && sl>0)
   {
      double risk=(side=="Buy" ? entry-sl : sl-entry);
      double reward=(side=="Buy" ? exit_price-entry : entry-exit_price);
      if(risk>0) r=reward/risk;
   }

   string j="{";
   j+="\"source_id\":\"mt5:"+IntegerToString(login)+":"+IntegerToString((long)deal)+"\",";
   j+="\"account\":\""+IntegerToString(login)+"\",";
   j+="\"deal\":\""+IntegerToString((long)deal)+"\",";
   j+="\"position_id\":\""+IntegerToString(position_id)+"\",";
   j+="\"symbol\":\""+JsonEscape(symbol)+"\",";
   j+="\"side\":\""+side+"\",";
   j+="\"entry\":"+Num(entry)+",";
   j+="\"sl\":"+Num(sl)+",";
   j+="\"target\":"+Num(tp)+",";
   j+="\"exit\":"+Num(exit_price)+",";
   j+="\"qty\":"+Num(volume,4)+",";
   j+="\"pnl\":"+Num(pnl,2)+",";
   j+="\"r\":"+Num(r,3)+",";
   j+="\"commission\":"+Num(commission+fee,2)+",";
   j+="\"swap\":"+Num(swap,2)+",";
   j+="\"close_time\":"+IntegerToString((long)close_time)+",";
   j+="\"comment\":\""+JsonEscape(comment)+"\"";
   j+="}";
   return j;
}

void SyncJournal()
{
   if(StringLen(SyncKey)<16 || StringFind(SyncKey,"PASTE_")==0)
   {
      Print("RupakJournalSync: Journal Sync Key paste karo.");
      return;
   }

   datetime to=TimeCurrent();
   datetime from=to-(datetime)(HistoryDays*86400);
   if(!HistorySelect(from,to))
   {
      Print("RupakJournalSync: HistorySelect failed ",GetLastError());
      return;
   }

   int total=HistoryDealsTotal();
   string body="{\"trades\":[";
   int count=0;
   for(int i=total-1;i>=0 && count<MaxDealsPerSync;i--)
   {
      ulong deal=HistoryDealGetTicket(i);
      if(deal==0) continue;
      string t=MakeTradeJson(deal);
      if(t=="") continue;
      if(count>0) body+=",";
      body+=t;
      count++;
   }
   body+="]}";
   if(count==0) return;

   string url=BridgeURL+"/mt5/journal/"+SyncKey;
   string headers="Content-Type: application/json\r\n";
   char data[];
   int len=StringToCharArray(body,data,0,WHOLE_ARRAY,CP_UTF8);
   if(len>0) ArrayResize(data,len-1);
   char result[];
   string result_headers;
   ResetLastError();
   int code=WebRequest("POST",url,headers,7000,data,result,result_headers);
   if(code==-1)
   {
      Print("RupakJournalSync WebRequest failed. Error=",GetLastError(),". MT5 Tools > Options > Expert Advisors me BridgeURL allow karo: ",BridgeURL);
      return;
   }
   Print("RupakJournalSync: HTTP ",code," | deals sent=",count);
}

int OnInit()
{
   EventSetTimer(MathMax(10,SyncEverySeconds));
   SyncJournal();
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   SyncJournal();
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}
