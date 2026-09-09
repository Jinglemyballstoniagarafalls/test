import os
import json
import time
import sys
import shutil
from collections import deque
from datetime import datetime
from typing import Dict, List, Any, Optional, Set, Tuple
from dataclasses import dataclass

# import requests   # not used, can be removed if you like
from flask import Flask, request, jsonify, send_from_directory, render_template_string, render_template

# ------------------------------------------------------------
# Only ONE Flask app and ONE Api instance
# ------------------------------------------------------------
app = Flask(__name__, static_folder='static')
api = Api()   # defined below

# --------------------------------------------
# HELPER FUNCTIONS (unchanged)
# --------------------------------------------
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    YELLOW = '\033[93m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'

def validate_json_file(filepath):
    if not os.path.exists(filepath):
        return True, None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return True, data
    except Exception as e:
        print(f"[WARN] Corrupt JSON file: {filepath} ({e})")
        return False, None

def backup_corrupt_file(filepath):
    if not os.path.exists(filepath):
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{filepath}.corrupt.{timestamp}"
    shutil.move(filepath, backup_path)
    print(f"[INFO] Renamed corrupt file: {filepath} -> {backup_path}")

def atomic_save(data, filepath):
    temp_path = filepath + ".tmp"
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, filepath)

# --------------------------------------------
# UserData & UserDatabase (unchanged)
# --------------------------------------------
@dataclass
class UserData:
    username: str
    rockstar_id: str
    data: Dict[str, Any]
    source_file: str

class UserDatabase:
    def __init__(self):
        self.files: Dict[str, Dict[str, UserData]] = {}
        self.users_by_rid: Dict[str, List[UserData]] = {}
        self.users_by_name: Dict[str, List[UserData]] = {}
        self.users_by_linked_service: Dict[str, List[UserData]] = {}
        self.users_by_crew: Dict[str, List[UserData]] = {}
        self._reverse_friends_index: Optional[Dict[str, List[UserData]]] = None

    def _build_reverse_friends_index(self):
        reverse = {}
        for fname, users_dict in self.files.items():
            for username, userdata in users_dict.items():
                friends = self._extract_friends_from_data(userdata.data)
                for friend in friends:
                    friend_rid = str(friend.get("rockstarId"))
                    if friend_rid:
                        reverse.setdefault(friend_rid, []).append(userdata)
        self._reverse_friends_index = reverse

    def get_reverse_friends_index(self) -> Dict[str, List[UserData]]:
        if self._reverse_friends_index is None:
            self._build_reverse_friends_index()
        return self._reverse_friends_index

    def _extract_rockstar_id(self, user_data: Dict[str, Any]) -> Optional[str]:
        try:
            api_response = user_data.get("api_response", {})
            accounts = api_response.get("accounts", [])
            if accounts and isinstance(accounts, list):
                rockstar_account = accounts[0].get("rockstarAccount", {})
                rid = rockstar_account.get("rockstarId")
                if rid is not None:
                    return str(rid)
        except (KeyError, IndexError, AttributeError):
            pass
        rid = user_data.get("rockstar_id")
        if rid is not None:
            return str(rid)
        try:
            api_response = user_data.get("api_response", {})
            rid = api_response.get("viewerRockstarId")
            if rid is not None:
                return str(rid)
        except (KeyError, AttributeError):
            pass
        return None

    def _extract_friends_from_data(self, user_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        api_response = user_data.get("api_response", {})
        accounts = api_response.get("accounts", [])
        if accounts and isinstance(accounts, list) and len(accounts) > 0:
            account = accounts[0]
            friends = account.get("friends")
            if friends is not None:
                return friends
        friends = user_data.get("friends")
        return friends if friends is not None else []

    def _get_linked_accounts(self, user_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        api_response = user_data.get("api_response", {})
        accounts = api_response.get("accounts", [])
        if accounts and isinstance(accounts, list) and len(accounts) > 0:
            return accounts[0].get("linkedAccounts", [])
        return []

    def _get_crews(self, user_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        api_response = user_data.get("api_response", {})
        accounts = api_response.get("accounts", [])
        if accounts and isinstance(accounts, list) and len(accounts) > 0:
            return accounts[0].get("crews", [])
        return []

    def load_file(self, filepath: str) -> bool:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            filename = os.path.basename(filepath)
            user_map = {}
            for top_username, top_user_data in data.items():
                rid = self._extract_rockstar_id(top_user_data)
                if rid:
                    user_map[rid] = (top_username, top_user_data, filename)
                else:
                    print(f"Warning: top-level user {top_username} has no rockstar_id, skipping as main user")
                friends_list = self._extract_friends_from_data(top_user_data)
                for friend in friends_list:
                    friend_rid = str(friend.get("rockstarId"))
                    if friend_rid and friend_rid not in user_map:
                        friend_username = friend.get("name") or friend.get("displayName") or f"User_{friend_rid}"
                        user_map[friend_rid] = (friend_username, friend, filename)
            if filename in self.files:
                self.unload_file(filename)
            new_file_data = {}
            rid_index = {}
            name_index = {}
            linked_index = {}
            crew_index = {}
            for rid, (username, user_data, fname) in user_map.items():
                if not username:
                    username = f"User_{rid}"
                user = UserData(
                    username=username,
                    rockstar_id=rid,
                    data=user_data,
                    source_file=fname
                )
                new_file_data[username] = user
                rid_index.setdefault(rid, []).append(user)
                name_index.setdefault(username.lower(), []).append(user)
                for acc in self._get_linked_accounts(user_data):
                    service = acc.get("onlineService")
                    if service:
                        linked_index.setdefault(service, []).append(user)
                for crew in self._get_crews(user_data):
                    name = crew.get("crewName")
                    tag = crew.get("crewTag")
                    if name:
                        crew_index.setdefault(name, []).append(user)
                    if tag:
                        crew_index.setdefault(tag, []).append(user)
            self.files[filename] = new_file_data
            for rid, users in rid_index.items():
                self.users_by_rid.setdefault(rid, []).extend(users)
            for name, users in name_index.items():
                self.users_by_name.setdefault(name, []).extend(users)
            for service, users in linked_index.items():
                self.users_by_linked_service.setdefault(service, []).extend(users)
            for crew, users in crew_index.items():
                self.users_by_crew.setdefault(crew, []).extend(users)
            self._reverse_friends_index = None
            print(f"Loaded {len(user_map)} unique users from {filename}")
            return True
        except Exception as e:
            print(f"Error loading {filepath}: {e}")
            return False

    def unload_file(self, filename: str):
        if filename not in self.files:
            return
        for username, user in self.files[filename].items():
            self._remove_from_index(self.users_by_rid, user.rockstar_id, filename)
            self._remove_from_index(self.users_by_name, username.lower(), filename)
            for acc in self._get_linked_accounts(user.data):
                service = acc.get("onlineService")
                if service:
                    self._remove_from_index(self.users_by_linked_service, service, filename)
            for crew in self._get_crews(user.data):
                name = crew.get("crewName")
                tag = crew.get("crewTag")
                if name:
                    self._remove_from_index(self.users_by_crew, name, filename)
                if tag:
                    self._remove_from_index(self.users_by_crew, tag, filename)
        del self.files[filename]
        self._reverse_friends_index = None

    def _remove_from_index(self, index: Dict[str, List[UserData]], key: str, filename: str):
        if key in index:
            index[key] = [u for u in index[key] if u.source_file != filename]
            if not index[key]:
                del index[key]

    def search_by_rid(self, rid: str) -> Dict[str, List[UserData]]:
        results = {}
        rid_str = str(rid).strip()
        for user in self.users_by_rid.get(rid_str, ()):
            results.setdefault(user.source_file, []).append(user)
        return results

    def search_by_username(self, username: str) -> Dict[str, List[UserData]]:
        results = {}
        seen: Set[Tuple[str, str, str]] = set()
        query = username.lower().strip()
        for uname_lower, users in self.users_by_name.items():
            if query in uname_lower:
                for user in users:
                    key = (user.source_file, user.rockstar_id, user.username)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.setdefault(user.source_file, []).append(user)
        return results

    def get_filtered_users(self, linked_service: Optional[str] = None, crew: Optional[str] = None) -> List[UserData]:
        result_users = None
        if linked_service:
            result_users = self.users_by_linked_service.get(linked_service, [])
        if crew:
            crew_users = self.users_by_crew.get(crew, [])
            if result_users is None:
                result_users = crew_users
            else:
                crew_set = {u.rockstar_id for u in crew_users}
                result_users = [u for u in result_users if u.rockstar_id in crew_set]
        if result_users is None:
            all_users = []
            for fname, users_dict in self.files.items():
                all_users.extend(users_dict.values())
            return all_users
        return result_users

    def search_with_filters(self, query: str, linked_service: Optional[str] = None, crew: Optional[str] = None) -> Dict[str, List[UserData]]:
        filtered_users = self.get_filtered_users(linked_service, crew)
        if not query:
            results = {}
            for user in filtered_users:
                results.setdefault(user.source_file, []).append(user)
            return results
        query_lower = query.lower().strip()
        matched = []
        for user in filtered_users:
            if query_lower in user.username.lower() or user.rockstar_id == query:
                matched.append(user)
        results = {}
        for user in matched:
            results.setdefault(user.source_file, []).append(user)
        return results

    def get_filter_options(self) -> Dict[str, Any]:
        return {
            "linked_services": sorted(self.users_by_linked_service.keys()),
            "crews": sorted(self.users_by_crew.keys())
        }

    def get_loaded_files(self) -> List[str]:
        return list(self.files.keys())

    def get_user_count(self) -> int:
        return sum(len(users) for users in self.files.values())

# --------------------------------------------
# API CLASS (unchanged)
# --------------------------------------------
class Api:
    def __init__(self):
        self.db = UserDatabase()
        self.advanced_results = {
            "stored": None,
            "sc_api": None,
            "sc_cache": None,
            "career_stats": None
        }
        # Automatically load users.json if present
        if os.path.exists('users.json'):
            print("[STARTUP] Loading users.json...")
            if self.db.load_file('users.json'):
                print(f"[STARTUP] Loaded {self.db.get_user_count()} users.")
            else:
                print("[STARTUP] Failed to load users.json")
        else:
            print("[STARTUP] users.json not found. Please add it to the repo.")

    def _format_results(self, results: Dict[str, List[UserData]]) -> Dict[str, Any]:
        formatted = {}
        for filename, users in results.items():
            formatted[filename] = []
            for user in users:
                formatted[filename].append({
                    "username": user.username,
                    "rockstar_id": user.rockstar_id,
                    "friends_count": len(self.db._extract_friends_from_data(user.data)),
                    "last_scanned": user.data.get("last_scanned"),
                    "source_file": user.source_file,
                    "raw_data": user.data
                })
        return formatted

    def search_by_rid(self, rid: str) -> Dict[str, Any]:
        results = self.db.search_by_rid(rid)
        return self._format_results(results)

    def search_by_username(self, username: str) -> Dict[str, Any]:
        results = self.db.search_by_username(username)
        return self._format_results(results)

    def get_filter_options(self) -> Dict[str, Any]:
        return self.db.get_filter_options()

    def search_with_filters(self, query: str, linked_service: str, crew: str) -> Dict[str, Any]:
        linked = None if linked_service == "All" else linked_service
        crew_name = None if crew == "All" else crew
        results = self.db.search_with_filters(query, linked, crew_name)
        return self._format_results(results)

    def get_user_details(self, username: str, filename: str) -> Optional[Dict]:
        if filename in self.db.files and username in self.db.files[filename]:
            user = self.db.files[filename][username]
            return {
                "username": user.username,
                "rockstar_id": user.rockstar_id,
                "source_file": user.source_file,
                "data": user.data
            }
        return None

    def get_deduced_friends(self, username: str, filename: str) -> Dict[str, List[Dict[str, str]]]:
        if filename not in self.db.files or username not in self.db.files[filename]:
            return {}
        viewed_user = self.db.files[filename][username]
        viewed_rid = viewed_user.rockstar_id
        reverse_index = self.db.get_reverse_friends_index()
        deduced = {}
        seen: Set[Tuple[str, str]] = set()
        for userdata in reverse_index.get(viewed_rid, []):
            if userdata.username == username and userdata.source_file == filename:
                continue
            key = (userdata.source_file, userdata.username)
            if key in seen:
                continue
            seen.add(key)
            deduced.setdefault(userdata.source_file, []).append({
                "username": userdata.username,
                "rockstar_id": userdata.rockstar_id
            })
        return deduced

    # ---- Graph ----
    def get_ego_graph(self, username: str, filename: str, depth: int = 2, root_rid: Optional[str] = None) -> Dict[str, Any]:
        try:
            if filename not in self.db.files or username not in self.db.files[filename]:
                return {"error": "User not found in database."}
            
            start_user = self.db.files[filename][username]
            actual_root_rid = root_rid if root_rid else start_user.rockstar_id
            
            root_user = start_user
            if root_rid and root_rid != start_user.rockstar_id:
                root_users = self.db.users_by_rid.get(root_rid, [])
                if root_users:
                    root_user = root_users[0]

            visited_rids: Set[str] = set()
            nodes: List[Dict] = []
            edges: List[Dict] = []
            queue = deque()
            node_degrees: Dict[str, int] = {}
            added_node_ids: Set[str] = set()
            
            nodes.append({
                "id": root_user.rockstar_id,
                "label": root_user.username,
                "type": "self",
                "degree": 0,
                "source_file": root_user.source_file,
                "username": root_user.username,
                "rockstar_id": root_user.rockstar_id
            })
            added_node_ids.add(root_user.rockstar_id)
            visited_rids.add(root_user.rockstar_id)
            node_degrees[root_user.rockstar_id] = 0
            
            root_friends = self.db._extract_friends_from_data(root_user.data)
            for friend in root_friends:
                friend_rid = str(friend.get("rockstarId"))
                if friend_rid and friend_rid not in visited_rids:
                    friend_users = self.db.users_by_rid.get(friend_rid, [])
                    if friend_users:
                        friend_user = friend_users[0]
                        friend_friends = self.db._extract_friends_from_data(friend_user.data)
                        has_normal_friends = len(friend_friends) > 0
                        visited_rids.add(friend_rid)
                        node_degrees[friend_rid] = 1
                        queue.append((friend_user, 1, root_user.rockstar_id, "friend", has_normal_friends))
            
            if len(root_friends) == 0:
                reverse_index = self.db.get_reverse_friends_index()
                for ded_user in reverse_index.get(root_user.rockstar_id, []):
                    if ded_user.rockstar_id not in visited_rids:
                        visited_rids.add(ded_user.rockstar_id)
                        node_degrees[ded_user.rockstar_id] = 1
                        queue.append((ded_user, 1, root_user.rockstar_id, "deduced", False))

            MAX_NODES = 5000
            warning = None
            reverse_index = self.db.get_reverse_friends_index()

            while queue and len(nodes) < MAX_NODES:
                user, current_depth, source_rid, edge_type, has_normal_friends = queue.popleft()
                
                if user.rockstar_id not in added_node_ids:
                    nodes.append({
                        "id": user.rockstar_id,
                        "label": user.username,
                        "type": "friend" if edge_type == "friend" else "deduced",
                        "degree": current_depth,
                        "source_file": user.source_file,
                        "username": user.username,
                        "rockstar_id": user.rockstar_id
                    })
                    added_node_ids.add(user.rockstar_id)
                
                edges.append({
                    "source": source_rid,
                    "target": user.rockstar_id,
                    "type": edge_type
                })

                if current_depth >= depth:
                    continue

                if has_normal_friends:
                    friends = self.db._extract_friends_from_data(user.data)
                    for friend in friends:
                        friend_rid = str(friend.get("rockstarId"))
                        if not friend_rid or friend_rid in visited_rids:
                            continue
                        friend_users = self.db.users_by_rid.get(friend_rid, [])
                        if friend_users:
                            friend_user = friend_users[0]
                            friend_friends = self.db._extract_friends_from_data(friend_user.data)
                            friend_has_normal = len(friend_friends) > 0
                            visited_rids.add(friend_rid)
                            node_degrees[friend_rid] = current_depth + 1
                            queue.append((friend_user, current_depth + 1, user.rockstar_id, "friend", friend_has_normal))
                        else:
                            friend_label = friend.get("name") or f"User_{friend_rid}"
                            if friend_rid not in added_node_ids:
                                nodes.append({
                                    "id": friend_rid,
                                    "label": friend_label,
                                    "type": "unknown",
                                    "degree": current_depth + 1,
                                    "source_file": None,
                                    "username": friend_label,
                                    "rockstar_id": friend_rid
                                })
                                added_node_ids.add(friend_rid)
                            edges.append({
                                "source": user.rockstar_id,
                                "target": friend_rid,
                                "type": "friend"
                            })
                            visited_rids.add(friend_rid)

            if len(nodes) >= MAX_NODES:
                warning = f"Graph truncated to {MAX_NODES} nodes. Consider reducing depth."

            return {
                "nodes": nodes,
                "edges": edges,
                "root_rid": actual_root_rid,
                "warning": warning
            }
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Graph generation error: {str(e)}"}

    def change_graph_root(self, rid: str) -> Dict[str, Any]:
        try:
            users = self.db.users_by_rid.get(rid, [])
            if not users:
                return {"error": "User not found in database."}
            user = users[0]
            filename = user.source_file
            return self.get_ego_graph(user.username, filename, depth=2, root_rid=rid)
        except Exception as e:
            return {"error": f"Failed to change root: {str(e)}"}

    # ---- Pathfinder ----
    def find_connection_paths(self, start_rid: str, end_rid: str, max_depth: int = 5, max_paths: int = 3) -> Dict[str, Any]:
        try:
            start_users = self.db.users_by_rid.get(start_rid, [])
            end_users = self.db.users_by_rid.get(end_rid, [])
            
            if not start_users:
                return {"error": f"Start user with RID {start_rid} not found."}
            if not end_users:
                return {"error": f"End user with RID {end_rid} not found."}
            
            if start_rid == end_rid:
                return {"error": "Start and end user are the same."}
            
            start_user = start_users[0]
            end_user = end_users[0]
            
            queue = deque([(start_rid, [start_rid])])
            visited = {start_rid: 0}
            parents = {start_rid: []}
            paths_found = []
            
            while queue and len(paths_found) < max_paths:
                current_rid, path = queue.popleft()
                current_depth = len(path) - 1
                
                if current_depth >= max_depth:
                    continue
                
                current_users = self.db.users_by_rid.get(current_rid, [])
                if not current_users:
                    continue
                current_user = current_users[0]
                
                friends = self.db._extract_friends_from_data(current_user.data)
                friend_rids = [str(f.get("rockstarId")) for f in friends if f.get("rockstarId")]
                
                if len(friend_rids) == 0:
                    reverse_index = self.db.get_reverse_friends_index()
                    for ded_user in reverse_index.get(current_rid, []):
                        if ded_user.rockstar_id not in friend_rids:
                            friend_rids.append(ded_user.rockstar_id)
                
                for friend_rid in friend_rids:
                    if friend_rid == end_rid:
                        complete_path = path + [friend_rid]
                        paths_found.append(complete_path)
                        if len(paths_found) >= max_paths:
                            break
                        continue
                    
                    if friend_rid not in visited or visited[friend_rid] == current_depth + 1:
                        if friend_rid not in visited:
                            visited[friend_rid] = current_depth + 1
                            queue.append((friend_rid, path + [friend_rid]))
                        if friend_rid not in parents:
                            parents[friend_rid] = []
                        parents[friend_rid].append(current_rid)
            
            if not paths_found:
                paths_found = self._dfs_find_paths(start_rid, end_rid, max_depth, max_paths)
            
            if not paths_found:
                return {
                    "error": f"No connection found within {max_depth} hops.",
                    "start": {"rid": start_rid, "username": start_user.username},
                    "end": {"rid": end_rid, "username": end_user.username}
                }
            
            nodes = {}
            edges = []
            
            for path_idx, path in enumerate(paths_found):
                for i, rid in enumerate(path):
                    if rid not in nodes:
                        user_list = self.db.users_by_rid.get(rid, [])
                        user = user_list[0] if user_list else None
                        nodes[rid] = {
                            "id": rid,
                            "label": user.username if user else f"User_{rid}",
                            "username": user.username if user else f"User_{rid}",
                            "source_file": user.source_file if user else None,
                            "rockstar_id": rid,
                            "type": "start" if rid == start_rid else "end" if rid == end_rid else "intermediate",
                            "in_paths": []
                        }
                    nodes[rid]["in_paths"].append(path_idx)
                    
                    if i < len(path) - 1:
                        edges.append({
                            "source": rid,
                            "target": path[i + 1],
                            "path_index": path_idx
                        })
            
            return {
                "nodes": list(nodes.values()),
                "edges": edges,
                "paths": paths_found,
                "path_count": len(paths_found),
                "start": {"rid": start_rid, "username": start_user.username},
                "end": {"rid": end_rid, "username": end_user.username},
                "shortest_length": min(len(p) - 1 for p in paths_found) if paths_found else None
            }
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Path finding error: {str(e)}"}

    def _dfs_find_paths(self, start_rid: str, end_rid: str, max_depth: int, max_paths: int) -> List[List[str]]:
        paths = []
        def dfs(current_rid: str, path: List[str], depth: int):
            if len(paths) >= max_paths:
                return
            if depth > max_depth:
                return
            if current_rid == end_rid:
                paths.append(path[:])
                return
            users = self.db.users_by_rid.get(current_rid, [])
            if not users:
                return
            user = users[0]
            friends = self.db._extract_friends_from_data(user.data)
            friend_rids = [str(f.get("rockstarId")) for f in friends if f.get("rockstarId")]
            if len(friend_rids) == 0:
                reverse_index = self.db.get_reverse_friends_index()
                for ded_user in reverse_index.get(current_rid, []):
                    if ded_user.rockstar_id not in friend_rids:
                        friend_rids.append(ded_user.rockstar_id)
            for friend_rid in friend_rids:
                if friend_rid not in path:
                    path.append(friend_rid)
                    dfs(friend_rid, path, depth + 1)
                    path.pop()
        dfs(start_rid, [start_rid], 0)
        return paths

    # ---- Advanced (stubbed) ----
    def get_advanced_results(self) -> Dict[str, Any]:
        return self.advanced_results

    def clear_advanced_results(self):
        self.advanced_results = {
            "stored": None,
            "sc_api": None,
            "sc_cache": None,
            "career_stats": None
        }

    def run_advanced_scan(self, username: str, filename: str, options: Dict[str, bool]) -> Dict[str, Any]:
        stored_user = None
        if filename in self.db.files and username in self.db.files[filename]:
            stored_user = self.db.files[filename][username]
        if not stored_user:
            return {"error": "User not found in database."}

        self.clear_advanced_results()
        self.advanced_results["stored"] = {
            "username": stored_user.username,
            "rockstar_id": stored_user.rockstar_id,
            "data": stored_user.data,
            "source_file": stored_user.source_file
        }

        self.advanced_results["sc_api"] = {"error": "sc-api requires browser automation, not available in web version."}
        self.advanced_results["sc_cache"] = {"error": "sc-cache requires browser automation, not available in web version."}
        self.advanced_results["career_stats"] = {"error": "Career stats require browser automation, not available in web version."}
        
        return {"success": True, "warning": "Browser-based scans are disabled. Only stored data was loaded."}

# ------------------------------------------------------------
# ROUTES
# ------------------------------------------------------------
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/app')
def app_ui():
    # IMPORTANT: template name fixed to 'acq.html'
    return render_template('acq.html')

# ---- File management (only get endpoints, no upload) ----
@app.route('/api/get_loaded_files')
def get_loaded_files():
    return jsonify(api.db.get_loaded_files())

@app.route('/api/get_user_count')
def get_user_count():
    return jsonify(api.db.get_user_count())

# ---- Search ----
@app.route('/api/search_by_rid')
def search_by_rid():
    rid = request.args.get('rid')
    results = api.db.search_by_rid(rid)
    return jsonify(api._format_results(results))

@app.route('/api/search_by_username')
def search_by_username():
    username = request.args.get('username')
    results = api.db.search_by_username(username)
    return jsonify(api._format_results(results))

@app.route('/api/search_with_filters')
def search_with_filters():
    query = request.args.get('query', '')
    linked = request.args.get('linked_service', 'All')
    crew = request.args.get('crew', 'All')
    linked = None if linked == 'All' else linked
    crew_name = None if crew == 'All' else crew
    results = api.db.search_with_filters(query, linked, crew_name)
    return jsonify(api._format_results(results))

@app.route('/api/get_filter_options')
def get_filter_options():
    return jsonify(api.db.get_filter_options())

# ---- User details ----
@app.route('/api/get_user_details')
def get_user_details():
    username = request.args.get('username')
    filename = request.args.get('filename')
    user = api.db.files.get(filename, {}).get(username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({
        'username': user.username,
        'rockstar_id': user.rockstar_id,
        'source_file': user.source_file,
        'data': user.data
    })

@app.route('/api/get_deduced_friends')
def get_deduced_friends():
    username = request.args.get('username')
    filename = request.args.get('filename')
    result = api.get_deduced_friends(username, filename)
    return jsonify(result)

# ---- Graph ----
@app.route('/api/get_ego_graph')
def get_ego_graph():
    username = request.args.get('username')
    filename = request.args.get('filename')
    depth = int(request.args.get('depth', 2))
    root_rid = request.args.get('root_rid')
    result = api.get_ego_graph(username, filename, depth, root_rid)
    return jsonify(result)

@app.route('/api/change_graph_root', methods=['POST'])
def change_graph_root():
    data = request.json
    rid = data.get('rid')
    result = api.change_graph_root(rid)
    return jsonify(result)

# ---- Pathfinder ----
@app.route('/api/find_connection_paths', methods=['POST'])
def find_connection_paths():
    data = request.json
    start_rid = data.get('start_rid')
    end_rid = data.get('end_rid')
    max_depth = data.get('max_depth', 5)
    max_paths = data.get('max_paths', 3)
    result = api.find_connection_paths(start_rid, end_rid, max_depth, max_paths)
    return jsonify(result)

# ---- Advanced (stubbed) ----
@app.route('/api/get_advanced_results')
def get_advanced_results():
    return jsonify(api.get_advanced_results())

@app.route('/api/clear_advanced_results', methods=['POST'])
def clear_advanced_results():
    api.clear_advanced_results()
    return jsonify({'status': 'ok'})

@app.route('/api/run_advanced_scan', methods=['POST'])
def run_advanced_scan():
    data = request.json
    username = data.get('username')
    filename = data.get('filename')
    options = data.get('options', {})
    result = api.run_advanced_scan(username, filename, options)
    return jsonify(result)

# ---- Scanner (stubbed) ----
@app.route('/api/open_browser', methods=['POST'])
def open_browser():
    return jsonify({'message': 'Browser automation is disabled in web version.'})

@app.route('/api/get_token_from_browser', methods=['POST'])
def get_token():
    return jsonify({'error': 'Token acquisition requires browser automation, not available.'})

@app.route('/api/start_scan', methods=['POST'])
def start_scan():
    return jsonify({'error': 'Scanner disabled in web version.'})

@app.route('/api/stop_scan', methods=['POST'])
def stop_scan():
    return jsonify({'message': 'Scanner disabled.'})

@app.route('/api/get_scan_status')
def scan_status():
    return jsonify('Idle')

# --------------------------------------------
# RUN
# --------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)