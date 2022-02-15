from sqlite3.dbapi2 import Error
from py2neo import Graph
from datetime import datetime
import sys
import argparse
import pandas as pd
import logging
from collections import Counter
import sqlite3
import hashlib

def banner():
    print("""   ______                ____  __                      __""")
    print("""  / ____/___  ____  ____/ / / / /___  __  ______  ____/ /""")
    print(""" / / __/ __ \/ __ \/ __  / /_/ / __ \/ / / / __ \/ __  / """)
    print("""/ /_/ / /_/ / /_/ / /_/ / __  / /_/ / /_/ / / / / /_/ /  """)
    print(  "\____/\____/\____/\__,_/_/ /_/\____/\__,_/_/ /_/\__,_/   """)

def arguments():
    argparser = argparse.ArgumentParser(description="BloodHound Wrapper to determine the Busiest Attack Paths to High Value targets.", add_help=True, epilog="Attackers think in graphs, Defenders think in actions, Management think in charts.")
    parsegroupdb = argparser.add_argument_group('Neo4jConnection')
    parsegroupdb.add_argument("-u", "--username", default="neo4j", help="Neo4j Database Username (Default: neo4j)", type=str)
    parsegroupdb.add_argument("-p", "--password", default="neo4j", help="Neo4j Database Password (Default: neo4j)", type=str)
    parsegroupdb.add_argument("-s", "--server", default="bolt://localhost:7687", help="Neo4j server Default: bolt://localhost:7687)", type=str)
    parsegroupoutput = argparser.add_argument_group('Output Formats')
    parsegroupoutput.add_argument("-o", "--output-format", default="csv", help="Output formats supported: stdout, csv, md (markdown). Default: csv.", type=str, choices=["stdout", "csv", "md", "markdown"])
    parsegroupoutput.add_argument("-f", "--output-filepath", default=".", help="File path to save the csv output. Defaults to current directory.", type=str)
    parsegroupoutput.add_argument("-v", "--verbose", help="Enables verbose output.", action="store_true")
    parsegroupqueryparams = argparser.add_argument_group('Query Parameters')
    parsegroupqueryparams.add_argument("-r", "--results", default="5", help="The number of busiest paths to process. The higher the number the longer the query will take. Default: 5", type=int)
    parsegroupqueryparams.add_argument("-sort", "--sort", default="risk", help="Option to sort results by number of users with the path, number of hops or risk score. Default: Risk Score", type=str, choices=["users", "hops", "risk"])
    parsegroupqueryparams.add_argument("-q", "--query", help="Optionally add a custom query to replace the default busiest paths query. This can be used to run a query that perhaps does not take as long as the full run. The format should maintain the 'match p=shortestpath((g:Group)-[]->(n)) return distinct(g.name) as groupname, min(length(p)) as hops' structure so that it doesn't derp up the rest of the script. e.g. 'match p=shortestpath((g:Group {highvalue:FALSE})-[*1..]->(n {highvalue:TRUE})) WHERE tolower(g.name) =~ 'admin.*' return distinct(g.name) as groupname, min(length(p)) as hops'", type=str)
    parsegroupschema = argparser.add_argument_group('Schema')
    parsegroupschema.add_argument("-sch", "--schema", help="Optionally select a text file containing custom cypher queries to add labels to the neo4j database. e.g. Use this if you want to add the highvalue label to assets that do not have this by default in the BloodHound schema.", type=str)
    parsegroupsql = argparser.add_argument_group('SQLite Database')
    parsegroupsql.add_argument("--db-skip", help="Skips the logging of attack paths to a local SQLite Database", action="store_true")
    parsegroupsql.add_argument("-sqlpath", "--sql-path", default="goodhound.db", help="Sets the location of the SQLite Database", type=str)
    args = argparser.parse_args()
    return args

def db_connect(args):
    logging.info('Connecting to database.')
    try:
        graph = Graph(args.server, user=args.username, password=args.password)
        return graph    
    except:
        logging.warning("Database connection failure.")
        sys.exit(1)

def schema(graph, args):
    try:
        with open(args.schema,'r') as schema_query:
            line = schema_query.readline()
            logging.info('Writing schema.')
            while line:
                graph.run(line)
                line = schema_query.readline()
        logging.info("Written schema!")        
        return()
    except:
        logging.warning("Error setting custom schema.")
        sys.exit(1)

def bloodhound41patch(graph):
    """Sharphound 4.1 doesn't automatically tag non highvalue items with the attribute."""
    hvuserpatch="""match (u:User) where u.highvalue is NULL set u.highvalue = FALSE"""
    graph.run(hvuserpatch)
    hvgrouppatch="""match (g:Group) where u.highvalue is NULL set g.highvalue = FALSE"""
    graph.run(hvgrouppatch)
    return()

def cost(graph):
    cost=["MATCH (n)-[r:MemberOf]->(m:Group) SET r.pwncost = 0",
    "MATCH (n)-[r:HasSession]->(m) SET r.pwncost = 3",
    "MATCH (n)-[r:CanRDP|Contains|GpLink]->(m) SET r.pwncost = 0",
    "MATCH (n)-[r:AdminTo|ForceChangePassword|AllowedToDelegate|AllowedToAct|AddAllowedToAct|ReadLAPSPassword|ReadGMSAPassword|HasSidHistory]->(m) SET r.pwncost = 1",
    "MATCH (n)-[r:CanPSRemote|ExecuteDCOM|SQLAdmin ]->(m) SET r.pwncost = 1",
    "MATCH (n)-[r:AllExtendedRights|AddMember|AddMembers|GenericAll|WriteDacl|WriteOwner|Owns|GenericWrite]->(m:Group) SET r.pwncost = 1",
    "MATCH (n)-[r:AllExtendedRights|GenericAll|WriteDacl|WriteOwner|Owns|GenericWrite]->(m:User) SET r.pwncost = 1",
    "MATCH (n)-[r:AllExtendedRights|GenericAll|WriteDacl|WriteOwner|Owns|GenericWrite]->(m:Computer) SET r.pwncost = 1",
    "MATCH (n)-[r:DCSync|GetChanges|GetChangesAll|AllExtendedRights|GenericAll|WriteDacl|WriteOwner|Owns]->(m:Domain) SET r.pwncost = 2",
    "MATCH (n)-[r:GenericAll|WriteDacl|WriteOwner|Owns|GenericWrite]->(m:GPO) SET r.pwncost = 1",
    "MATCH (n)-[r:GenericAll|WriteDacl|WriteOwner|Owns|GenericWrite ]->(m:OU) SET r.pwncost = 1"]
    print("Setting cost.")
    try:
        for c in cost:
            graph.run(c)
        return()
    except:
        logging.warning("Error setting cost!")
        sys.exit(1)

def shortestpath(graph, starttime, args):
    """
    Runs a shortest path query for all AD groups to high value targets. Returns a list of groups.
    Respect to the Plumhound project https://github.com/PlumHound/PlumHound and BloodhoundGang Slack channel https://bloodhoundhq.slack.com for the influence and assistance with this.
    """
    if args.query:
        query_shortestpath=f"%s" %args.query
    else:
        query_shortestpath="""match p=shortestpath((g:Group {highvalue:FALSE})-[:MemberOf|HasSession|AdminTo|AllExtendedRights|AddMember|ForceChangePassword|GenericAll|GenericWrite|Owns|WriteDacl|WriteOwner|CanRDP|ExecuteDCOM|AllowedToDelegate|ReadLAPSPassword|Contains|GpLink|AddAllowedToAct|AllowedToAct|SQLAdmin|ReadGMSAPassword|HasSIDHistory|CanPSRemote*1..]->(n {highvalue:TRUE})) with reduce(totalscore = 0, rels in relationships(p) | totalscore + rels.pwncost) as cost, length(p) as hops, g.name as groupname, [node in nodes(p) | coalesce(node.name, "")] as nodeLabels, [rel in relationships(p) | type(rel)] as relationshipLabels with reduce(path="", x in range(0,hops-1) | path + nodeLabels[x] + " - " + relationshipLabels[x] + " -> ") as path, nodeLabels[hops] as final_node, hops as hops, groupname as groupname, cost as cost, nodeLabels as nodeLabels, relationshipLabels as relLabels return groupname, hops, min(cost) as cost, nodeLabels, relLabels, path + final_node as full_path"""
    print("Running query, this may take a while.")
    try:
        groupswithpath=graph.run(query_shortestpath).data()
    except:
        logging.warning("There is a problem with the inputted query. If you have entered a custom query check the syntax.")
        sys.exit(1)
    querytime = round((datetime.now()-starttime).total_seconds() / 60)
    logging.info("Finished query in : {} Minutes".format(querytime))
    return groupswithpath

def totalusers(graph):
    """Calculate the total users in the dataset."""
    totalenablednonadminsquery="""match (u:User {highvalue:FALSE, enabled:TRUE}) return count(u)"""
    totalenablednonadminusers = int(graph.run(totalenablednonadminsquery).evaluate())
    return totalenablednonadminusers

def getmaxcost(groupswithpath):
    """Get the maximum amount of hops in the dataset to be used as part of the risk score calculation"""
    maxhops=[]
    for sublist in groupswithpath:
        maxhops.append(sublist.get('hops'))
    maxcost = (max(maxhops))*3+1
    return maxcost

def getdirectgroupmembers(graph, uniquegroupswithpath):
    """Gets a list of direct group members for every group with a path"""
    totalgroupswithpath = len(uniquegroupswithpath)
    #grouploopstart = datetime.now()
    print("Counting Users in Groups")
    groupswithmembers = []
    i=0
    for group in uniquegroupswithpath:
        print (f"Finding direct members of {group} - {i} of {totalgroupswithpath}..................................", end="\r")
        i +=1
        query_group_members = """match (u:User {highvalue:FALSE, enabled:TRUE})-[:MemberOf]->(g:Group {name:"%s"}) return distinct(u.name) as members""" % group
        group_members = graph.run(query_group_members).data()
        num_members = len(group_members)
        members = [m.get('members') for m in group_members if num_members != 0]
        groupwithdirectmembers = {"groupname":group, "groupdirectmembers":members}
        groupswithmembers.append(groupwithdirectmembers)
    return groupswithmembers

def getuniquegroupswithpath(groupswithpath):
    """Gets a unique list of groups with a path"""
    uniquegroupswithpath=[]
    for g in groupswithpath:
        group = g.get('groupname')
        if group not in uniquegroupswithpath:
            uniquegroupswithpath.append(group)
    return uniquegroupswithpath

#def getuniquelistitems(lst):
#    uniquelst = []
#    for i in lst:
#        if i not in uniquelst:
#            uniquelst.append(i)
#    return uniquelst

def mergedirectandindirect(groupswithmembers):
    """Combines the distinct directmembers and the indirectmembers of the group into a single list"""
    for g in groupswithmembers:
        combined = []
        directmembers = g.get('groupdirectmembers')
        indirectmembers = g.get('indirectmembers')
        for m in directmembers:
            if m not in combined:
                combined.append(m)
        for m in indirectmembers:
            if m not in combined:
                combined.append(m)
        g["combined"]=combined


def getindirectgroupmembers(graph, groupswithmembers):
    """Gets a list of indirect group members for every group with a path and appends them to the list"""
    for g in groupswithmembers:
        group = g.get('groupname')
        print (f"Finding indirect members of {group}........................................", end="\r")
        nestedgroupsquery = """match (ng:Group {highvalue:FALSE})-[:MemberOf*1..]->(g:Group {name:"%s"}) return ng.name as nestedgroups""" %group
        nestedgroups = graph.run(nestedgroupsquery).data()
        num_nestedgroups = len(nestedgroups)
        indirectmembers = []
        if num_nestedgroups !=0: 
            for ng in nestedgroups:
                nestedgroup = ng.get('nestedgroups')
                #I would imagine that nested groups should already be in the groupswithpath, but just in case they're not.
                if nestedgroup not in groupswithmembers:
                    query_group_members = """match (u:User {highvalue:FALSE, enabled:TRUE})-[:MemberOf]->(g:Group {name:"%s"}) return distinct(u.name) as members""" % nestedgroup
                    group_members = graph.run(query_group_members).data()
                    num_members = len(group_members)
                    members = [m.get('members') for m in group_members if num_members != 0]
                    indirectmembers.append(members)
                else:
                    # This crazy comprehension finds the index value of the group in the groupswithdirectmembers list
                    nestedgroupindex = next((index for (index, groupname) in enumerate(groupswithmembers) if groupname["groupname"] == "%s"), None) %nestedgroup
                    members = groupswithmembers[nestedgroupindex]['groupdirectmembers']
                    indirectmembers.append(members)
        # flatten the list of lists before committing
        indirectmemberslst = [m for im in indirectmembers for m in im]
        g["indirectmembers"]=indirectmemberslst
    mergedirectandindirect(groupswithmembers)
    return groupswithmembers
        
def generateresults(groupswithpath, groupswithmembers, totalenablednonadminusers):
    """combine the output of the paths query and the groups query"""
    maxcost = getmaxcost(groupswithpath)
    results = []
    for g in groupswithpath:
        group = g.get('groupname')
        hops = g.get('hops')
        cost = g.get('cost')
        fullpath = g.get('full_path')
        endnode = g.get('nodeLabels')[-1]
        query = bh_query(group, hops, endnode)
        uid = hashlib.md5(fullpath.encode()).hexdigest()
        if cost == None:
            # While debugging this should highlight edges without a score assigned. CHECK ON THIS LOGIC, I'M NOT SURE IT'S CORRECT.
            logging.info(f"Null edge cost found with {group} and {hops} hops.")
            cost = 0
        # find the index of the relative group in groupswithmembers and pull results
        groupindex = next((index for (index, groupname) in enumerate(groupswithmembers) if groupname["groupname"] == group), None)
        num_members = len(groupswithmembers[groupindex]['combined'])
        percentage=round(float((num_members/totalenablednonadminusers)*100), 1)
        riskscore = round((((maxcost-cost)/maxcost)*percentage),1)
        result = [group, num_members, percentage, hops, cost, riskscore, fullpath, query, uid]
        results.append(result)
    return results

def gettotaluniqueuserswithpath(groupswithmembers):
    uniqueusers=[]
    for g in groupswithmembers:
        members = g.get("combined")
        for m in members:
            if m not in uniqueusers:
                uniqueusers.append(m)
    totaluniqueuserswithpath = len(uniqueusers)
    return totaluniqueuserswithpath

def getuniqueresults(results):
    """This stops many paths appearing in the result from the same group which can happen. This doesn't feel like the best way of approaching this and should be looked at for improvement."""
    uniquegroupresults = []
    #sort by groupname and then risk score in order to take the top risk score result for each group with a path
    sorted_p = sorted(results, key=lambda i: (i[0], -i[5]))
    for p in sorted_p:
        group = p[0]
        num_members = p[1]
        percentage = p[2]
        hops = p[3]
        cost = p[4]
        riskscore = p[5]
        fullpath = p[6]
        query = p[7]
        uid = p[8]
        # check if there is already a path added for the current group and if not add it.
        if (len(uniquegroupresults)==0) or (any(group == ugp[0] for ugp in uniquegroupresults) != True):
            unique = [group, num_members, percentage, hops, cost, riskscore, fullpath, query, uid]
            uniquegroupresults.append(unique)
    return uniquegroupresults

def sortresults(args, results):
    """Sorts the results depending on the argument selected. By default this is by Risk Score.
    Also takes the number of results selected in the arguments. Default is 5."""
    if args.sort == 'users':
        top_results = (sorted(results, key=lambda i: -i[2])[0:args.results])
    elif args.sort == 'hops':
        top_results = (sorted(results, key=lambda i: i[3])[0:args.results])
    else:
        top_results = (sorted(results, key=lambda i: (-i[5], i[4], i[3]))[0:args.results])
    return top_results

#def busiestpath(groupswithpath, totalenablednonadminusers, graph, args):
#    """Calculate the busiest paths by getting the number of users in the Groups that have a path to Highvalue, sorting the result, calculating some statistics and returns a list."""
#    totalpaths = len(groupswithpath)
#    paths=[]
#    users=[]
#    i=0
#    maxcost = getmaxcost(groupswithpath)
#    grouploopstart = datetime.now()
#    print("Counting Users in Groups")
#    for g in groupswithpath:
#        i +=1
#        group = g.get('groupname')
#        hops = g.get('hops')
#        cost = g.get('cost')
#        fullpath = g.get('full_path')
#        endnode = g.get('nodeLabels')[-1]
#        uid = hashlib.md5(fullpath.encode()).hexdigest()
#        if cost == None:
#            # While debugging this should highlight edges without a score assigned.
#            logging.info(f"Null edge cost found with {group} and {hops} hops.")
#            cost = 0
#        # Establishes if the group has already had the number of group members counted and skips it if so
#        if (len(paths)==0) or (any(group == path[0] for path in paths) != True):
#            print (f"Processing path {i} of {totalpaths}", end="\r")
#            query_group_members = """match (u:User {highvalue:FALSE, enabled:TRUE})-[:MemberOf*1..]->(g:Group {name:"%s"}) return distinct(u.name) as members""" % group
#            group_members = graph.run(query_group_members).data()
#            num_members = len(group_members)
#            if len(group_members) != 0:
#                for m in group_members:
#                    member = m.get('members')
#                    users.append(member)
#            percentage=round(float((num_members/totalenablednonadminusers)*100), 1)
#            riskscore = round((((maxcost-cost)/maxcost)*percentage),1)
#            result = [group, num_members, percentage, hops, cost, riskscore, fullpath, endnode, uid]
#            paths.append(result)
#        else:
#            print (f"Processing path {i} of {totalpaths}", end="\r")
#            for path in paths:
#                if path[0] == group:
#                    num_members = path[1]
#                    percentage = path[2]
#                    riskscore = round((((maxcost-cost)/maxcost)*percentage),1)
#                    result = [group, num_members, percentage, hops, cost, riskscore, fullpath, endnode, uid]
#                    paths.append(result)
#                    break
#    print("\n")
#    # Calls the bh_query function to add the bloodhound path to the result
#    allresults = bh_query(paths)
#    # Removes duplicate starting groups from the results
#    unique_groupswpath = []
#    sorted_p = sorted(allresults, key=lambda i: (i[0], -i[5]))
#    for p in sorted_p:
#        group = p[0]
#        num_members = p[1]
#        percentage = p[2]
#        hops = p[3]
#        cost = p[4]
#        riskscore = p[5]
#        fullpath = p[6]
#        query = p[7]
#        uid = p[8]
#        if (len(unique_groupswpath)==0) or (any(group == ugp[0] for ugp in unique_groupswpath) != True):
#            unique = [group, num_members, percentage, hops, cost, riskscore, fullpath, query, uid]
#            unique_groupswpath.append(unique)
#    if args.sort == 'users':
#        top_paths = (sorted(unique_groupswpath, key=lambda i: -i[2])[0:args.results])
#    elif args.sort == 'hops':
#        top_paths = (sorted(unique_groupswpath, key=lambda i: i[3])[0:args.results])
#    else:
#        top_paths = (sorted(unique_groupswpath, key=lambda i: (-i[5], i[4], i[3]))[0:args.results])
#    # Processes the output into a dataframe
#    total_unique_users = len((pd.Series(users, dtype="O")).unique())
#    total_users_percentage = round(((total_unique_users/totalenablednonadminusers)*100),1)
#    grandtotals = [{"Total Non-Admins with a Path":total_unique_users, "Percentage of Total Enabled Non-Admins":total_users_percentage, "Total Paths":totalpaths}]
#    grouploopfinishtime = datetime.now()
#    grouplooptime = round((grouploopfinishtime-grouploopstart).total_seconds() / 60)
#    logging.info("Finished counting users in: {} minutes.".format(grouplooptime))
#    return top_paths, grandtotals, totalpaths, allresults

def weakestlinks(groupswithpath, totalpaths):
    """Attempts to determine the most common weak links across all attack paths"""
    links = []
    for path in groupswithpath:
        nodes = path.get('nodeLabels')
        rels = path.get('relLabels')
        # assembles the nodes and rels into a chain
        chain = sum(zip(nodes, rels+[0]), ())[:-1]
        # Divides the chains into Node-Rel-Node-Rel-Node groups as attack paths are usually "This can do that to the other. The other can then do this."
        for c in chain[:-4:2]:
            endlink = int(chain.index(c))+5
            link = []
            for ch in chain[chain.index(c):endlink]:
                link.append(ch)
            # Makes it into a neat string
            link = '->'.join(link)
            links.append(link)
    common_link = list(Counter(links).most_common(5))
    weakest_links = []
    for x in common_link:
        l = list(x)
        pct = round(l[1]/totalpaths*100,1)
        l.append(pct)
        weakest_links.append(l)
    return weakest_links

    
def bh_query(group, hops, endnode):
    """Generate a replayable query for each finding for Bloodhound visualisation."""
    previous_hop = hops-1
    query = """match p=((g:Group {name:'%s'})-[*%s..%s]->(n {name:'%s'})) return p""" %(group, previous_hop, hops, endnode)
    return query

def grandtotals(totaluniqueuserswithpath, totalenablednonadminusers, totalpaths, new_path, seen_before, weakest_links, top_results):
    total_users_percentage = round(((totaluniqueuserswithpath/totalenablednonadminusers)*100),1)
    grandtotals = [{"Total Non-Admins with a Path":totaluniqueuserswithpath, "Percentage of Total Enabled Non-Admins":total_users_percentage, "Total Paths":totalpaths, "% of Paths Seen Before":seen_before/totalpaths*100, "New Paths":new_path}]
    grandtotalsdf = pd.DataFrame(grandtotals)
    weakest_linkdf = pd.DataFrame(weakest_links, columns=["Weakest Link", "Number of Paths it appears in", "% of Total Paths"])
    busiestpathsdf = pd.DataFrame(top_results, columns=["Starting Group", "Number of Enabled Non-Admins with Path", "Percent of Total Enabled Non-Admins with Path", "Number of Hops", "Exploit Cost", "Risk Score", "Path", "Bloodhound Query", "UID"])
    return grandtotalsdf, weakest_linkdf, busiestpathsdf

def output(args, grandtotalsdf, weakest_linkdf, busiestpathsdf, scandatenice, starttime):
    finish = datetime.now()
    totalruntime = round((finish - starttime).total_seconds() / 60)
    logging.info("Total runtime: {} minutes.".format(totalruntime))
    pd.set_option('display.max_colwidth', None)
    if args.output_format == "stdout":
        print("\n\nGRAND TOTALS")
        print("============")
        print(grandtotalsdf.to_string(index=False))
        print("\nBUSIEST PATHS")
        print("-------------\n")
        print (busiestpathsdf.to_string(index=False))
        print("-------------\n")
        print("\nTHE WEAKEST LINKS")
        print (weakest_linkdf.to_string(index=False))
    elif args.output_format == ("md" or "markdown"):
        print("# GRAND TOTALS")
        print (grandtotalsdf.to_markdown(index=False))
        print("## BUSIEST PATHS")
        print (busiestpathsdf.to_markdown(index=False))
        print("## THE WEAKEST LINKS")
        print (weakest_linkdf.to_markdown(index=False))
    else:
        summaryname = f"{args.output_filepath}\\" + f"{scandatenice}" + "_GoodHound_summary.csv"
        busiestpathsname = f"{args.output_filepath}\\" + f"{scandatenice}" + "_GoodHound_busiestpaths.csv"
        weakestlinkname = f"{args.output_filepath}\\" + f"{scandatenice}" + "_GoodHound_weakestlinks.csv"
        grandtotalsdf.to_csv(summaryname, index=False)
        busiestpathsdf.to_csv(busiestpathsname, index=False)
        weakest_linkdf.to_csv(weakestlinkname, index=False)
        print("CSV reports written to selected file path.")

#def output(top_results, grandtotals, totalpaths, args, starttime, new_path, seen_before, weakest_links, scandatenice):
#    finish = datetime.now()
#    totalruntime = round((finish - starttime).total_seconds() / 60)
#    logging.info("Total runtime: {} minutes.".format(totalruntime))
#    pd.set_option('display.max_colwidth', None)
#    grandtotals[0]["% of Paths Seen Before"] = seen_before/totalpaths*100
#    grandtotals[0]["New Paths"] = new_path
#    totaldf = pd.DataFrame(grandtotals)
#    weakest_linkdf = pd.DataFrame(weakest_links, columns=["Weakest Link", "Number of Paths it appears in", "% of Total Paths"])
#    resultsdf = pd.DataFrame(top_results, columns=["Starting Group", "Number of Enabled Non-Admins with Path", "Percent of Total Enabled Non-Admins with Path", "Number of Hops", "Exploit Cost", "Risk Score", "Path", "Bloodhound Query", "UID"])
#    if args.output_format == "stdout":
#        print("\n\nGRAND TOTALS")
#        print("============")
#        print(totaldf.to_string(index=False))
#        print("\nBUSIEST PATHS")
#        print("-------------\n")
#        print (resultsdf.to_string(index=False))
#        print("-------------\n")
#        print("\nTHE WEAKEST LINKS")
#        print (weakest_linkdf.to_string(index=False))
#    elif args.output_format == ("md" or "markdown"):
#        print("# GRAND TOTALS")
#        print (totaldf.to_markdown(index=False))
#        print("## BUSIEST PATHS")
#        print (resultsdf.to_markdown(index=False))
#        print("## THE WEAKEST LINKS")
#        print (weakest_linkdf.to_markdown(index=False))
#    else:
#        summaryname = f"{args.output_filepath}\\" + f"{scandatenice}" + "_GoodHound_summary.csv"
#        busiestpathsname = f"{args.output_filepath}\\" + f"{scandatenice}" + "_GoodHound_busiestpaths.csv"
#        weakestlinkname = f"{args.output_filepath}\\" + f"{scandatenice}" + "_GoodHound_weakestlinks.csv"
#        totaldf.to_csv(summaryname, index=False)
#        resultsdf.to_csv(busiestpathsname, index=False)
#        weakest_linkdf.to_csv(weakestlinkname, index=False)

def getscandate(graph):
    """Find the date that the Sharphound collection was run based on the most recent lastlogondate timestamp of the Domain Controllers"""
    scandate_query="""WITH '(?i)ldap/.*' as regex_one WITH '(?i)gc/.*' as regex_two MATCH (n:Computer) WHERE ANY(item IN n.serviceprincipalnames WHERE item =~ regex_two OR item =~ regex_two ) return n.lastlogontimestamp as date order by date desc limit 1"""
    scandate = int(graph.run(scandate_query).evaluate())
    scandatenice = (datetime.fromtimestamp(scandate)).strftime("%Y-%m-%d")
    return scandate, scandatenice

def db(results, graph, args):
    """Inserts all of the attack paths found into a SQLite database"""
    if not args.db_skip:
        table_sql = """CREATE TABLE IF NOT EXISTS paths (
    	uid TEXT PRIMARY KEY,
    	groupname TEXT NOT NULL,
    	num_users INTEGER NOT NULL,
    	percentage REAL NOT NULL,
    	hops INTEGER NOT NULL,
    	cost INTEGER NOT NULL,
        riskscore REAL NOT NULL,
        fullpath TEXT NOT NULL,
        query TEXT NOT NULL,
        first_seen INTEGER NOT NULL,
    	last_seen INTEGER NOT NULL);"""
        conn = None
        try:
            conn = sqlite3.connect(args.sql_path)
            c = conn.cursor()
            c.execute(table_sql)
            scandate, scandatenice = getscandate(graph)
            seen_before=0
            new_path=0
            for r in results:
                insertvalues = (r[8],r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7],scandate,scandate,)
                insertpath_sql = 'INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?,?,?)'
                # Determines if the path has been previously logged in the database using the UID and updates the last_seen field
                updatevalues = {"last_seen":scandate, "uid":r[8]}
                updatepath_sql = 'UPDATE paths SET last_seen=:last_seen WHERE uid=:uid'
                # Determines if the path has not been seen before based on the UID and inserts it into the database
                c.execute("SELECT count(*) FROM paths WHERE uid = ?", (r[8],))
                data = c.fetchone()[0]
                if data==0:
                    c.execute(insertpath_sql, insertvalues)
                    new_path += 1
                else:
                    # Catch to stop accidentally overwriting the database with older data
                    c.execute("SELECT last_seen from paths WHERE uid = ?", (r[8],))
                    pathlastseen = int(c.fetchone()[0])
                    if pathlastseen < scandate:
                        c.execute(updatepath_sql, updatevalues)
                    # update first_seen if an older dataset is loaded in
                    c.execute("SELECT first_seen from paths WHERE uid = ?", (r[8],))
                    pathfirstseen = int(c.fetchone()[0])
                    if pathfirstseen > scandate:
                        c.execute("UPDATE paths SET first_seen=:first_seen WHERE uid=:uid", {"first_seen":scandate, "uid":r[8]})
                    seen_before += 1
            conn.commit()
        except Error as e:
            print(e)
        finally:
            if conn:
                conn.close()
    else:
        new_path = 0
        seen_before = 0
        scandate, scandatenice = getscandate(graph)
    return new_path, seen_before, scandatenice


def main():
    args = arguments()
    if args.verbose:
        logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.INFO)
    banner()
    graph = db_connect(args)
    starttime = datetime.now()
    if args.schema:
        schema(graph, args)
    cost(graph)
    bloodhound41patch(graph)
    groupswithpath = shortestpath(graph, starttime, args)
    totalenablednonadminusers = totalusers(graph)
    uniquegroupswithpath = getuniquegroupswithpath(groupswithpath)
    groupswithmembers = getdirectgroupmembers(graph, uniquegroupswithpath)
    groupswithmembers = getindirectgroupmembers(graph, groupswithmembers)
    totaluniqueuserswithpath = gettotaluniqueuserswithpath(groupswithmembers)
    results = generateresults(groupswithpath, groupswithmembers, totalenablednonadminusers)
    new_path, seen_before, scandatenice = db(results, graph, args)
    uniqueresults = getuniqueresults(results)
    top_results = sortresults(args, uniqueresults)
    #top_results, grandtotals, totalpaths, allresults = busiestpath(groupswithpath, totalenablednonadminusers, graph, args)
    totalpaths = len(groupswithpath)
    weakest_links = weakestlinks(groupswithpath, totalpaths)
    grandtotalsdf, weakest_linkdf, busiestpathsdf = grandtotals(totaluniqueuserswithpath, totalenablednonadminusers, totalpaths, new_path, seen_before, weakest_links, top_results)
    output(args, grandtotalsdf, weakest_linkdf, busiestpathsdf, scandatenice, starttime)
    #output(top_results, grandtotals, totalpaths, args, starttime, new_path, seen_before, weakest_links, scandatenice)
    #if not args.db_skip:
    #    new_path, seen_before, scandatenice = db(allresults, graph, args)
    #    output(top_paths, grandtotals, totalpaths, args, starttime, new_path, seen_before, weakest_links, scandatenice)
    #else:
    #    new_path = 0
    #    seen_before = 0
    #    scandate_query="""WITH '(?i)ldap/.*' as regex_one WITH '(?i)gc/.*' as regex_two MATCH (n:Computer) WHERE ANY(item IN n.serviceprincipalnames WHERE item =~ regex_two OR item =~ regex_two ) return n.lastlogontimestamp as date order by date desc limit 1"""
    #    scandate = int(graph.run(scandate_query).evaluate())
    #    scandatenice = (datetime.fromtimestamp(scandate)).strftime("%Y-%m-%d")
    #    output(top_paths, grandtotals, totalpaths, args, starttime, new_path, seen_before, weakest_links, scandatenice)

if __name__ == "__main__":
    main()