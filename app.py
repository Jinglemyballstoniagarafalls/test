import os
import json
import time
import sys
import shutil
from collections import deque
from datetime import datetime
from typing import Dict, List, Any, Optional, Set, Tuple
from dataclasses import dataclass

import requests
from flask import Flask, request, jsonify

# --------------------------------------------
# HELPER FUNCTIONS (same as before)
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
# API CLASS (unchanged, but we load file on init)
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

# --------------------------------------------
# HTML UI (removed upload button, added auto-load message)
# --------------------------------------------
def create_html():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Acquaintance™ Web</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,400;14..32,500;14..32,600;14..32,700&family=Orbitron:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg: #0d0d0f;
            --sidebar: #131316;
            --surface: #1a1a1e;
            --border: #2a2a2e;
            --text: #e8e8ec;
            --text-secondary: #8a8a94;
            --accent: #aaaaaa;
            --accent-hover: #c0c0c8;
            --radius: 8px;
            --transition: 0.2s ease;
        }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            display: flex;
        }
        .sidebar {
            width: 240px;
            height: 100vh;
            position: sticky;
            top: 0;
            background: var(--sidebar);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            padding: 20px 16px;
            flex-shrink: 0;
            overflow-y: auto;
        }
        .sidebar-logo {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 32px;
            padding: 4px 8px;
        }
        .sidebar-logo svg {
            width: 28px;
            height: 28px;
            flex-shrink: 0;
        }
        .sidebar-logo span {
            font-family: 'Orbitron', sans-serif;
            font-size: 18px;
            font-weight: 700;
            color: var(--text);
            letter-spacing: 0.5px;
        }
        .sidebar-nav {
            display: flex;
            flex-direction: column;
            gap: 4px;
            flex: 1;
        }
        .sidebar-nav .tab-btn {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 8px 12px;
            border-radius: var(--radius);
            color: var(--text-secondary);
            cursor: pointer;
            transition: background var(--transition), color var(--transition);
            font-size: 14px;
            font-weight: 500;
            border: none;
            background: transparent;
            width: 100%;
            text-align: left;
            font-family: 'Inter', sans-serif;
        }
        .sidebar-nav .tab-btn:hover {
            background: var(--surface);
            color: var(--text);
        }
        .sidebar-nav .tab-btn.active {
            background: var(--surface);
            color: var(--text);
            box-shadow: inset 3px 0 0 var(--text-secondary);
        }
        .sidebar-nav .tab-btn .icon {
            font-size: 18px;
            width: 24px;
            text-align: center;
        }
        .sidebar-footer {
            margin-top: auto;
            padding-top: 16px;
            border-top: 1px solid var(--border);
            font-size: 12px;
            color: var(--text-secondary);
        }
        .main {
            flex: 1;
            display: flex;
            flex-direction: column;
            min-height: 100vh;
            padding: 24px 32px;
            max-width: 1440px;
            margin: 0 auto;
            width: 100%;
        }
        .main-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            flex-wrap: wrap;
            gap: 12px;
        }
        .main-header .file-info {
            display: flex;
            align-items: center;
            gap: 16px;
            color: var(--text-secondary);
            font-size: 14px;
        }
        .main-header .file-info .total-users {
            font-weight: 600;
            color: var(--text);
        }
        .btn {
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 8px 18px;
            border-radius: 30px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: all var(--transition);
            font-family: 'Inter', sans-serif;
            white-space: nowrap;
        }
        .btn:hover {
            background: var(--border);
            border-color: var(--text-secondary);
        }
        .btn-primary {
            background: #D3D3D3;
            border-color: #D3D3D3;
            color: #000000;
        }
        .btn-primary:hover {
            background: #e8e8e8;
            border-color: #e8e8e8;
        }
        .btn-danger {
            background: rgba(255, 80, 80, 0.15);
            border-color: rgba(255, 80, 80, 0.3);
            color: #ff8a9e;
        }
        .btn-danger:hover {
            background: rgba(255, 80, 80, 0.25);
        }
        .btn-clear {
            background: rgba(255, 80, 80, 0.1);
            border-color: rgba(255, 80, 80, 0.2);
            color: #ff8a9e;
        }
        .btn-clear:hover {
            background: rgba(255, 80, 80, 0.2);
        }
        .btn-success {
            background: rgba(80, 200, 80, 0.15);
            border-color: rgba(80, 200, 80, 0.3);
            color: #80d080;
        }
        .btn-success:hover {
            background: rgba(80, 200, 80, 0.25);
        }
        .tab-content { display: none; }
        .tab-content.active { display: block; animation: fadeIn 0.2s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
        .search-section {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 20px 24px;
            margin-bottom: 24px;
        }
        .search-bar-container {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 12px;
        }
        .search-input {
            flex: 1;
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 10px 16px;
            border-radius: 30px;
            font-size: 14px;
            outline: none;
            transition: border var(--transition);
            font-family: 'Inter', sans-serif;
            min-width: 140px;
        }
        .search-input:focus { border-color: var(--text-secondary); }
        .search-input::placeholder { color: var(--text-secondary); }
        .search-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
        .filter-row {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            margin-top: 8px;
            padding-top: 12px;
            border-top: 1px solid var(--border);
        }
        .filter-select {
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 6px 12px;
            padding-right: 30px;
            border-radius: 30px;
            font-size: 13px;
            outline: none;
            transition: border var(--transition);
            font-family: 'Inter', sans-serif;
            appearance: none;
            -webkit-appearance: none;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%238a8a94' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 10px center;
            min-width: 120px;
            cursor: pointer;
        }
        .filter-select:focus { border-color: var(--text-secondary); }
        .filter-select option { background: var(--bg); }
        .crew-filter-btn {
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 6px 12px;
            border-radius: 30px;
            font-size: 13px;
            outline: none;
            transition: border var(--transition);
            font-family: 'Inter', sans-serif;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .crew-filter-btn:hover { border-color: var(--text-secondary); }
        .filter-status {
            color: var(--text-secondary);
            font-size: 12px;
            margin-left: auto;
        }
        .file-tags {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 12px;
        }
        .file-tag {
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 30px;
            padding: 4px 14px;
            font-size: 13px;
            display: flex;
            align-items: center;
            gap: 8px;
            color: var(--text-secondary);
        }
        .file-tag .remove { cursor: pointer; opacity: 0.6; transition: opacity var(--transition); font-size: 16px; line-height: 1; }
        .file-tag .remove:hover { opacity: 1; color: #ff8a9e; }
        .empty-state { color: var(--text-secondary); font-size: 13px; font-style: italic; }
        .results-wrapper { margin-bottom: 20px; }
        .results-container {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
            gap: 20px;
        }
        .results-panel {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            overflow: hidden;
        }
        .panel-header {
            background: var(--bg);
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .panel-title {
            font-weight: 600;
            color: var(--text);
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .panel-title::before {
            content: '';
            width: 6px;
            height: 6px;
            background: var(--text-secondary);
            border-radius: 50%;
            display: inline-block;
        }
        .result-count {
            background: var(--bg);
            color: var(--text-secondary);
            padding: 2px 10px;
            border-radius: 30px;
            font-size: 12px;
            border: 1px solid var(--border);
        }
        .panel-content { max-height: 600px; overflow-y: auto; }
        .user-card {
            border-bottom: 1px solid var(--border);
            padding: 12px 16px;
            cursor: pointer;
            transition: background var(--transition);
        }
        .user-card:hover { background: var(--bg); }
        .user-card:last-child { border-bottom: none; }
        .user-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 4px;
            margin-bottom: 4px;
        }
        .username { font-weight: 600; color: var(--text); font-size: 15px; }
        .rid {
            font-family: 'Inter', monospace;
            font-size: 12px;
            color: var(--text-secondary);
            background: var(--bg);
            padding: 2px 10px;
            border-radius: 20px;
            border: 1px solid var(--border);
        }
        .user-meta { display: flex; gap: 16px; font-size: 12px; color: var(--text-secondary); }
        .user-meta strong { color: var(--text); font-weight: 600; }
        .no-results { padding: 40px 20px; text-align: center; color: var(--text-secondary); font-size: 14px; }
        .pagination {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 12px;
            padding: 12px 0;
            margin-top: 12px;
            border-top: 1px solid var(--border);
        }
        .pagination button {
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 4px 16px;
            border-radius: 30px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: all var(--transition);
            font-family: 'Inter', sans-serif;
        }
        .pagination button:hover:not(:disabled) {
            background: var(--border);
            border-color: var(--text-secondary);
        }
        .pagination button:disabled { opacity: 0.3; cursor: not-allowed; }
        .pagination .page-info {
            color: var(--text-secondary);
            font-size: 14px;
            min-width: 100px;
            text-align: center;
        }
        #graphContainer, #pathfinderContainer {
            width: 100%;
            height: 70vh;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            position: relative;
            overflow: hidden;
        }
        #graphLegend, #pathfinderLegend {
            position: absolute;
            bottom: 16px;
            left: 16px;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 8px 12px;
            font-size: 13px;
            color: var(--text-secondary);
            display: flex;
            gap: 16px;
            z-index: 10;
        }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-color { width: 20px; height: 3px; border-radius: 2px; }
        .modal-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(4px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            padding: 20px;
            animation: fadeIn 0.2s ease;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            width: 100%;
            max-width: 800px;
            max-height: 85vh;
            display: flex;
            flex-direction: column;
            box-shadow: 0 20px 60px rgba(0,0,0,0.6);
            animation: slideUp 0.25s ease;
        }
        @keyframes slideUp { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
        .modal-header {
            background: var(--bg);
            border-bottom: 1px solid var(--border);
            padding: 16px 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .modal-title { font-weight: 700; font-size: 18px; color: var(--text); }
        .modal-close { background: none; border: none; color: var(--text-secondary); font-size: 24px; cursor: pointer; padding: 0 4px; transition: color var(--transition); }
        .modal-close:hover { color: #ff8a9e; }
        .modal-content { padding: 20px 24px; overflow-y: auto; flex: 1; }
        .detail-section { margin-bottom: 18px; }
        .detail-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-secondary); margin-bottom: 4px; font-weight: 600; }
        .detail-value { font-size: 14px; color: var(--text); background: var(--bg); padding: 8px 12px; border-radius: 6px; word-break: break-all; border: 1px solid var(--border); }
        .friends-list, .deduced-friends-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px; max-height: 340px; overflow-y: auto; }
        .friend-tag {
            background: var(--bg);
            border: 1px solid var(--border);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 13px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .friend-rid { font-size: 11px; color: var(--text-secondary); opacity: 0.7; }
        .crews-list { display: flex; flex-direction: column; gap: 6px; }
        .crew-tag { background: var(--bg); border: 1px solid var(--border); padding: 8px 12px; border-radius: 6px; font-size: 13px; }
        .crew-name { font-weight: 600; color: var(--text); }
        .crew-meta { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
        .games-list { display: flex; flex-wrap: wrap; gap: 6px; }
        .game-tag { background: var(--bg); border: 1px solid var(--border); padding: 4px 12px; border-radius: 30px; font-size: 12px; }
        .linked-accounts { display: flex; flex-direction: column; gap: 6px; }
        .account-tag { background: var(--bg); border: 1px solid var(--border); padding: 6px 12px; border-radius: 6px; font-size: 13px; display: flex; align-items: center; gap: 8px; }
        .account-service { font-weight: 600; color: var(--text-secondary); }
        .deduced-friends-section { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 16px; }
        .deduced-subheader { font-size: 13px; font-weight: 600; color: var(--text-secondary); margin: 8px 0 6px; }
        .scanner-controls { display: flex; flex-direction: column; gap: 16px; margin-bottom: 20px; }
        .scanner-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
        .scanner-input {
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 8px 16px;
            border-radius: 30px;
            font-size: 14px;
            outline: none;
            transition: border var(--transition);
            font-family: 'Inter', sans-serif;
            flex: 1 1 200px;
            min-width: 140px;
        }
        .scanner-input:focus { border-color: var(--text-secondary); }
        .scanner-checkbox { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); }
        .scanner-checkbox input[type="checkbox"] { accent-color: var(--text-secondary); width: 18px; height: 18px; cursor: pointer; }
        .scanner-log {
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 16px;
            max-height: 400px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 13px;
            color: #c8d0e0;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .scanner-log .log-line { padding: 2px 0; border-bottom: 1px solid var(--border); }
        .scanner-log .log-line.error { color: #ff8a9e; }
        .scanner-log .log-line.warning { color: #f0c060; }
        .scanner-log .log-line.success { color: #80d080; }
        .scanner-log .log-line.info { color: var(--text-secondary); }
        .advanced-container { display: flex; flex-direction: column; gap: 24px; }
        .advanced-section {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 16px 20px;
        }
        .advanced-section h3 {
            font-family: 'Orbitron', sans-serif;
            font-size: 16px;
            font-weight: 600;
            color: var(--text);
            margin-bottom: 12px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
        }
        .raw-json {
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 12px;
            font-family: 'Courier New', monospace;
            font-size: 13px;
            color: #c8d0e0;
            white-space: pre-wrap;
            word-break: break-all;
            max-height: 500px;
            overflow-y: auto;
        }
        .cache-item { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; margin-bottom: 6px; }
        .cache-item .label { color: var(--text-secondary); font-weight: 600; }
        .scan-options-modal {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(4px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 2000;
            padding: 20px;
        }
        .scan-options-modal.active { display: flex; }
        .scan-options-box {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 28px 32px;
            max-width: 500px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.6);
            animation: slideUp 0.25s ease;
        }
        .scan-options-box h3 { font-family: 'Orbitron', sans-serif; font-size: 18px; color: var(--text); margin-bottom: 16px; text-align: center; }
        .scan-option { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-radius: 6px; cursor: pointer; transition: background var(--transition); }
        .scan-option:hover { background: var(--bg); }
        .scan-option input[type="checkbox"] { accent-color: var(--text-secondary); width: 18px; height: 18px; cursor: pointer; }
        .scan-option label { color: var(--text); font-size: 15px; cursor: pointer; }
        .scan-option .desc { color: var(--text-secondary); font-size: 13px; margin-left: auto; }
        .scan-options-actions { display: flex; gap: 12px; margin-top: 20px; justify-content: flex-end; }
        .loading {
            display: none;
            position: fixed;
            top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 24px 32px;
            z-index: 3000;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0,0,0,0.6);
        }
        .loading.active { display: block; }
        .spinner {
            width: 36px; height: 36px;
            border: 3px solid var(--border);
            border-top-color: var(--text-secondary);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 12px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading span { font-size: 14px; color: var(--text-secondary); }
        .toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 30px;
            padding: 10px 24px;
            font-size: 13px;
            font-weight: 500;
            z-index: 4000;
            box-shadow: 0 8px 30px rgba(0,0,0,0.4);
            transform: translateY(80px);
            opacity: 0;
            transition: all 0.3s ease;
        }
        .toast.show { transform: translateY(0); opacity: 1; }
        .toast.error { border-color: #ff8a9e; color: #ff8a9e; }
        .toast.success { border-color: #80d080; color: #80d080; }
        @media (max-width: 768px) {
            .sidebar { width: 60px; padding: 16px 8px; }
            .sidebar-logo span { display: none; }
            .sidebar-logo svg { margin: 0 auto; }
            .sidebar-nav .tab-btn { justify-content: center; padding: 10px 0; font-size: 0; }
            .sidebar-nav .tab-btn .icon { font-size: 22px; width: auto; }
            .sidebar-footer { display: none; }
            .main { padding: 16px; }
            .main-header { flex-direction: column; align-items: stretch; gap: 8px; }
            .search-bar-container { flex-direction: column; }
            .search-buttons { justify-content: stretch; }
            .filter-row { flex-direction: column; align-items: stretch; }
            .filter-status { margin-left: 0; }
            .results-container { grid-template-columns: 1fr; }
            .modal { max-width: 100%; }
            #graphContainer, #pathfinderContainer { height: 50vh; }
        }
        @media (max-width: 480px) {
            .sidebar { width: 48px; padding: 12px 4px; }
            .sidebar-nav .tab-btn { padding: 8px 0; }
            .sidebar-nav .tab-btn .icon { font-size: 18px; }
        }
    </style>
</head>
<body>
    <!-- Sidebar -->
    <div class="sidebar">
        <div class="sidebar-logo">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" width="200" height="200" viewBox="0 0 100 100"><path fill="#FFFFFF" d="M1.22541 61.5228c-.2225-.9485.90748-1.5459 1.59638-.857L39.3342 97.1782c.6889.6889.0915 1.8189-.857 1.5964C20.0515 94.4522 5.54779 79.9485 1.22541 61.5228ZM.00189135 46.8891c-.01764375.2833.08887215.5599.28957165.7606L52.3503 99.7085c.2007.2007.4773.3075.7606.2896 2.3692-.1476 4.6938-.46 6.9624-.9259.7645-.157 1.0301-1.0963.4782-1.6481L2.57595 39.4485c-.55186-.5519-1.49117-.2863-1.648174.4782-.465915 2.2686-.77832 4.5932-.92588465 6.9624ZM4.21093 29.7054c-.16649.3738-.08169.8106.20765 1.1l64.77602 64.776c.2894.2894.7262.3742 1.1.2077 1.7861-.7956 3.5171-1.6927 5.1855-2.684.5521-.328.6373-1.0867.1832-1.5407L8.43566 24.3367c-.45409-.4541-1.21271-.3689-1.54074.1832-.99132 1.6684-1.88843 3.3994-2.68399 5.1855ZM12.6587 18.074c-.3701-.3701-.393-.9637-.0443-1.3541C21.7795 6.45931 35.1114 0 49.9519 0 77.5927 0 100 22.4073 100 50.0481c0 14.8405-6.4593 28.1724-16.7199 37.3375-.3903.3487-.984.3258-1.3542-.0443L12.6587 18.074Z"/></svg>
            <span>Acquaintance™</span>
        </div>
        <div class="sidebar-nav">
            <button class="tab-btn active" data-tab="viewer" onclick="switchTab('viewer')">
                <span class="icon">🕶️</span> Viewer
            </button>
            <button class="tab-btn" data-tab="graph" onclick="switchTab('graph')">
                <span class="icon">🗠</span> Graph
            </button>
            <button class="tab-btn" data-tab="pathfinder" onclick="switchTab('pathfinder')">
                <span class="icon">🔗</span> Pathfinder
            </button>
        </div>
        <div class="sidebar-footer">v9.0 Web</div>
    </div>

    <!-- Main -->
    <div class="main">
        <div class="main-header">
            <div class="file-info">
                <span id="loadedFiles">Loading...</span>
                <span class="total-users" id="totalUsers">—</span>
            </div>
            <!-- No upload button -->
        </div>

        <!-- Viewer Tab -->
        <div id="tab-viewer" class="tab-content active">
            <div class="search-section">
                <div class="search-bar-container">
                    <input type="text" class="search-input" id="searchInput" placeholder="Enter Rockstar ID or username..." onkeypress="if(event.key==='Enter')performSearch()">
                </div>
                <div class="search-buttons">
                    <button class="btn search-btn btn-primary" onclick="performSearch()">Search</button>
                    <button class="btn search-btn btn-primary" onclick="searchByRid()">Search by RID</button>
                    <button class="btn search-btn btn-primary" onclick="searchByUsername()">Search by Username</button>
                </div>
                <div class="filter-row">
                    <select id="filterLinked" class="filter-select">
                        <option value="All">All Linked Accounts</option>
                    </select>
                    <button class="crew-filter-btn" id="crewFilterBtn" onclick="openCrewModal()">
                        <span id="crewFilterLabel">All Crews</span>
                        <span style="font-size:10px;">▼</span>
                    </button>
                    <button class="btn btn-clear" onclick="clearFilters()">Clear Filters</button>
                    <span class="filter-status" id="filterStatus"></span>
                </div>
                <div class="file-tags" id="fileTags">
                    <span class="empty-state">No file loaded.</span>
                </div>
            </div>
            <div class="results-wrapper">
                <div class="results-container" id="resultsContainer"></div>
                <div class="pagination" id="paginationControls" style="display: none;">
                    <button id="prevPageBtn" onclick="prevPage()" disabled>Previous</button>
                    <span class="page-info" id="pageInfo">Page 1 of 1</span>
                    <button id="nextPageBtn" onclick="nextPage()" disabled>Next</button>
                </div>
            </div>
        </div>

        <!-- Graph Tab -->
        <div id="tab-graph" class="tab-content">
            <div class="search-section">
                <div class="search-bar-container">
                    <input type="text" class="search-input" id="graphSearchInput" placeholder="Enter username or RID to focus..." onkeypress="if(event.key==='Enter')renderGraph()">
                    <button class="btn btn-primary" onclick="renderGraph()">Render Graph</button>
                    <button class="btn btn-clear" onclick="clearGraph()">Clear</button>
                    <button class="btn" onclick="resetGraphRoot()" id="resetGraphBtn" style="display:none;">Reset to Original</button>
                </div>
                <div style="color: var(--text-secondary); font-size: 13px; margin-top: 8px; display: flex; gap: 20px; align-items: center;">
                    <span>Depth: <span id="graphDepthDisplay">2</span></span>
                    <input type="range" min="1" max="3" value="2" id="graphDepthSlider" style="width: 100px;" oninput="document.getElementById('graphDepthDisplay').textContent=this.value">
                    <span style="font-size: 12px; opacity: 0.7;">Click any node to center the graph on that person. Double-click to view profile.</span>
                </div>
            </div>
            <div id="graphContainer" style="width: 100%; height: 70vh; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); position: relative; overflow: hidden;">
                <div id="graphLegend" style="position: absolute; bottom: 16px; left: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 13px; color: var(--text-secondary); display: flex; gap: 16px; z-index: 10;">
                    <span class="legend-item" style="display:flex;align-items:center;gap:6px;">
                        <span style="width:12px;height:12px;border-radius:50%;background:#ff4444;"></span> Root
                    </span>
                    <span class="legend-item" style="display:flex;align-items:center;gap:6px;">
                        <span style="width:12px;height:12px;border-radius:50%;background:#4caf50;"></span> Direct Friends
                    </span>
                    <span class="legend-item" style="display:flex;align-items:center;gap:6px;">
                        <span style="width:12px;height:12px;border-radius:50%;background:#ffeb3b;"></span> Friends of Friends
                    </span>
                </div>
                <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: var(--text-secondary); pointer-events: none;">
                    Enter a username and click Render.
                </div>
            </div>
        </div>

        <!-- Pathfinder Tab -->
        <div id="tab-pathfinder" class="tab-content">
            <div class="search-section">
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;">
                    <div>
                        <label style="display: block; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px;">Person A (Start)</label>
                        <input type="text" class="search-input" id="pathStartInput" placeholder="Username or RID..." style="width: 100%;">
                    </div>
                    <div>
                        <label style="display: block; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px;">Person B (End)</label>
                        <input type="text" class="search-input" id="pathEndInput" placeholder="Username or RID..." style="width: 100%;">
                    </div>
                </div>
                <div style="display: flex; gap: 24px; align-items: center; flex-wrap: wrap; margin-bottom: 16px;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <label style="color: var(--text-secondary); font-size: 12px;">Max Depth (hops)</label>
                        <input type="range" min="2" max="10" value="5" id="pathDepthSlider" style="width: 120px;" oninput="document.getElementById('pathDepthDisplay').textContent=this.value">
                        <span id="pathDepthDisplay" style="color: var(--text); font-size: 13px; font-weight: 600;">5</span>
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <label style="color: var(--text-secondary); font-size: 12px;">Max Paths</label>
                        <input type="range" min="1" max="10" value="3" id="pathMaxPathsSlider" style="width: 100px;" oninput="document.getElementById('pathMaxPathsDisplay').textContent=this.value">
                        <span id="pathMaxPathsDisplay" style="color: var(--text); font-size: 13px; font-weight: 600;">3</span>
                    </div>
                    <div style="margin-left: auto; display: flex; gap: 8px;">
                        <button class="btn btn-primary" onclick="findConnectionPaths()">Find Connection</button>
                        <button class="btn btn-clear" onclick="clearPathfinder()">Clear</button>
                    </div>
                </div>
                <div id="pathfinderStatus" style="color: var(--text-secondary); font-size: 13px; min-height: 20px;"></div>
            </div>
            <div id="pathfinderContainer" style="width: 100%; height: 70vh; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); position: relative; overflow: hidden;">
                <div id="pathfinderLegend" style="position: absolute; bottom: 16px; left: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 13px; color: var(--text-secondary); display: flex; gap: 16px; z-index: 10;">
                    <span class="legend-item" style="display:flex;align-items:center;gap:6px;">
                        <span style="width:12px;height:12px;border-radius:50%;background:#4caf50;"></span> Start
                    </span>
                    <span class="legend-item" style="display:flex;align-items:center;gap:6px;">
                        <span style="width:12px;height:12px;border-radius:50%;background:#ff4444;"></span> End
                    </span>
                    <span class="legend-item" style="display:flex;align-items:center;gap:6px;">
                        <span style="width:12px;height:12px;border-radius:50%;background:#64b5f6;"></span> Connection
                    </span>
                </div>
                <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: var(--text-secondary); pointer-events: none; text-align: center;">
                    <div style="font-size: 48px; margin-bottom: 16px; opacity: 0.3;">🔗</div>
                    <div>Enter two users and click "Find Connection"</div>
                    <div style="font-size: 12px; margin-top: 8px; opacity: 0.7;">Discover how any two people are linked through mutual friends</div>
                </div>
            </div>
            <div id="pathResults" style="margin-top: 16px; display: none;">
                <div style="background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px;">
                    <div style="font-weight: 600; margin-bottom: 12px; color: var(--text);">Path Details</div>
                    <div id="pathResultsContent"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- Crew Modal -->
    <div class="modal-overlay" id="crewModal" onclick="if(event.target===this) closeCrewModal()">
        <div class="modal" style="max-width:500px;" onclick="event.stopPropagation()">
            <div class="modal-header">
                <div class="modal-title">Select Crew</div>
                <button class="modal-close" onclick="closeCrewModal()">&times;</button>
            </div>
            <div class="modal-content">
                <input type="text" class="search-input" id="crewSearchInput" placeholder="Search crews..." oninput="filterCrewList()" style="margin-bottom:12px;">
                <div id="crewListContainer" style="max-height:300px; overflow-y:auto;">
                    <div class="no-crews">Loading crews...</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Profile Modal -->
    <div class="modal-overlay" id="detailModal" onclick="if(event.target===this) closeModal()">
        <div class="modal" onclick="event.stopPropagation()">
            <div class="modal-header">
                <div class="modal-title" id="modalTitle">User Details</div>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-content" id="modalContent"></div>
        </div>
    </div>

    <div class="loading" id="loading"><div class="spinner"></div><span>Loading...</span></div>
    <div class="toast" id="toast"></div>

    <script src="https://cdn.jsdelivr.net/npm/graphology@0.25.1/dist/graphology.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/sigma@2.3.0/build/sigma.min.js"></script>
    <script>
        // ======================== Viewer JS ========================
        let currentFiles = [];
        let currentModalUser = null;
        let allCrews = [];
        let selectedCrew = 'All';
        const PAGE_SIZE = 250;
        let fullResults = null;
        let currentPage = 1;
        let totalPages = 1;
        let totalResults = 0;
        let sigmaInstance = null;
        let graphInstance = null;
        let pathfinderSigma = null;
        let pathfinderGraph = null;

        // ---------- File management (auto-load) ----------
        async function updateFileList() {
            try {
                const response = await fetch('/api/get_loaded_files');
                currentFiles = await response.json();
                const container = document.getElementById('fileTags');
                const fileCount = document.getElementById('loadedFiles');
                if (currentFiles.length === 0) {
                    container.innerHTML = '<span class="empty-state">No file loaded. Make sure users.json is in the repo.</span>';
                    fileCount.textContent = 'No file loaded';
                    document.getElementById('totalUsers').textContent = '—';
                } else {
                    fileCount.textContent = `${currentFiles.length} file(s) loaded`;
                    container.innerHTML = currentFiles.map(file => `
                        <div class="file-tag">
                            <span>${escapeHtml(file)}</span>
                        </div>
                    `).join('');
                }
                // Get user count
                const countResp = await fetch('/api/get_user_count');
                const count = await countResp.json();
                document.getElementById('totalUsers').textContent = `${count.toLocaleString()} users loaded`;
            } catch(e) { console.error(e); }
        }

        // ---------- Filter options ----------
        async function loadFilterOptions() {
            try {
                const response = await fetch('/api/get_filter_options');
                const options = await response.json();
                const linkedSelect = document.getElementById('filterLinked');
                const currentLinked = linkedSelect.value;
                linkedSelect.innerHTML = '<option value="All">All Linked Accounts</option>';
                for (const s of options.linked_services) {
                    const opt = document.createElement('option');
                    opt.value = s;
                    opt.textContent = s;
                    linkedSelect.appendChild(opt);
                }
                if (Array.from(linkedSelect.options).some(o => o.value === currentLinked)) {
                    linkedSelect.value = currentLinked;
                }
                allCrews = options.crews || [];
                document.getElementById('crewFilterLabel').textContent = selectedCrew === 'All' ? 'All Crews' : selectedCrew;
                if (selectedCrew !== 'All' && !allCrews.includes(selectedCrew)) {
                    selectedCrew = 'All';
                    document.getElementById('crewFilterLabel').textContent = 'All Crews';
                }
            } catch(e) { console.error(e); }
        }

        // ---------- Crew modal ----------
        function openCrewModal() {
            document.getElementById('crewModal').classList.add('active');
            document.getElementById('crewSearchInput').value = '';
            renderCrewList();
            document.getElementById('crewSearchInput').focus();
        }
        function closeCrewModal() { document.getElementById('crewModal').classList.remove('active'); }
        function renderCrewList() {
            const container = document.getElementById('crewListContainer');
            const search = document.getElementById('crewSearchInput').value.toLowerCase().trim();
            let filtered = allCrews;
            if (search) filtered = allCrews.filter(c => c.toLowerCase().includes(search));
            if (filtered.length === 0) { container.innerHTML = '<div class="no-crews">No crews found.</div>'; return; }
            let html = '';
            for (const crew of filtered) {
                const selectedClass = (crew === selectedCrew) ? 'selected' : '';
                html += `<div class="crew-item ${selectedClass}" onclick="selectCrew('${escapeHtml(crew)}')">${escapeHtml(crew)}</div>`;
            }
            container.innerHTML = html;
        }
        function filterCrewList() { renderCrewList(); }
        function selectCrew(crew) {
            selectedCrew = crew;
            document.getElementById('crewFilterLabel').textContent = crew;
            closeCrewModal();
        }

        // ---------- Search ----------
        async function performSearch() {
            const query = document.getElementById('searchInput').value.trim();
            const linked = document.getElementById('filterLinked').value;
            const crew = selectedCrew;
            showLoading();
            try {
                const url = `/api/search_with_filters?query=${encodeURIComponent(query)}&linked_service=${encodeURIComponent(linked)}&crew=${encodeURIComponent(crew)}`;
                const response = await fetch(url);
                const results = await response.json();
                fullResults = results;
                currentPage = 1;
                renderPage();
                document.getElementById('filterStatus').textContent = `Query: ${escapeHtml(query || 'All')} | Linked: ${escapeHtml(linked)} | Crew: ${escapeHtml(crew)}`;
            } catch(e) { showToast('Search failed', 'error'); }
            hideLoading();
        }

        async function searchByRid() {
            const query = document.getElementById('searchInput').value.trim();
            if (!query) { showToast('Enter a RID', 'error'); return; }
            showLoading();
            try {
                const response = await fetch(`/api/search_by_rid?rid=${encodeURIComponent(query)}`);
                const results = await response.json();
                fullResults = results;
                currentPage = 1;
                renderPage();
                document.getElementById('filterStatus').textContent = `RID: ${escapeHtml(query)}`;
            } catch(e) { showToast('Search failed', 'error'); }
            hideLoading();
        }

        async function searchByUsername() {
            const query = document.getElementById('searchInput').value.trim();
            if (!query) { showToast('Enter a username', 'error'); return; }
            showLoading();
            try {
                const response = await fetch(`/api/search_by_username?username=${encodeURIComponent(query)}`);
                const results = await response.json();
                fullResults = results;
                currentPage = 1;
                renderPage();
                document.getElementById('filterStatus').textContent = `Username: ${escapeHtml(query)}`;
            } catch(e) { showToast('Search failed', 'error'); }
            hideLoading();
        }

        async function clearFilters() {
            document.getElementById('filterLinked').value = 'All';
            selectedCrew = 'All';
            document.getElementById('crewFilterLabel').textContent = 'All Crews';
            document.getElementById('searchInput').value = '';
            document.getElementById('resultsContainer').innerHTML = `<div class="results-panel" style="grid-column: 1 / -1;"><div class="panel-header"><div class="panel-title">Filters Cleared</div></div><div class="panel-content"><div class="no-results">Filters have been reset. Click <strong>Search</strong> to view all users.</div></div></div>`;
            document.getElementById('paginationControls').style.display = 'none';
            document.getElementById('filterStatus').textContent = 'Filters cleared.';
            fullResults = null;
            currentPage = 1;
        }

        function renderPage() {
            if (!fullResults) { document.getElementById('resultsContainer').innerHTML = ''; document.getElementById('paginationControls').style.display = 'none'; return; }
            const allUsers = [];
            for (const fname in fullResults) {
                for (const user of fullResults[fname]) {
                    allUsers.push({ ...user, _file: fname });
                }
            }
            totalResults = allUsers.length;
            totalPages = Math.ceil(totalResults / PAGE_SIZE);
            if (totalPages === 0) {
                document.getElementById('resultsContainer').innerHTML = `<div class="results-panel" style="grid-column: 1 / -1;"><div class="panel-header"><div class="panel-title">No Results</div></div><div class="panel-content"><div class="no-results">No users found.</div></div></div>`;
                document.getElementById('paginationControls').style.display = 'none';
                return;
            }
            if (currentPage < 1) currentPage = 1;
            if (currentPage > totalPages) currentPage = totalPages;
            const start = (currentPage - 1) * PAGE_SIZE;
            const end = Math.min(start + PAGE_SIZE, totalResults);
            const pageUsers = allUsers.slice(start, end);
            const pageResults = {};
            for (const u of pageUsers) { const fname = u._file; if (!pageResults[fname]) pageResults[fname] = []; pageResults[fname].push(u); }
            const container = document.getElementById('resultsContainer');
            if (Object.keys(pageResults).length === 0) {
                container.innerHTML = `<div class="results-panel" style="grid-column: 1 / -1;"><div class="panel-header"><div class="panel-title">No Results</div></div><div class="panel-content"><div class="no-results">No users on this page.</div></div></div>`;
            } else {
                container.innerHTML = Object.entries(pageResults).map(([filename, users]) => `
                    <div class="results-panel">
                        <div class="panel-header"><div class="panel-title">${escapeHtml(filename)}</div><div class="result-count">${users.length}</div></div>
                        <div class="panel-content">${users.map(user => `
                            <div class="user-card" onclick="showDetails('${escapeHtml(user.username)}', '${escapeHtml(user.source_file)}')">
                                <div class="user-header"><div class="username">${escapeHtml(user.username)}</div><div class="rid">${escapeHtml(user.rockstar_id)}</div></div>
                                <div class="user-meta"><div class="meta-item"><span>Friends:</span><strong>${user.friends_count}</strong></div><div class="meta-item"><span>Scanned:</span><strong>${user.last_scanned ? new Date(user.last_scanned * 1000).toLocaleDateString() : 'N/A'}</strong></div></div>
                            </div>
                        `).join('')}</div>
                    </div>
                `).join('');
            }
            const pagDiv = document.getElementById('paginationControls');
            pagDiv.style.display = 'flex';
            document.getElementById('pageInfo').textContent = `Page ${currentPage} of ${totalPages} (${totalResults} results)`;
            document.getElementById('prevPageBtn').disabled = (currentPage <= 1);
            document.getElementById('nextPageBtn').disabled = (currentPage >= totalPages);
        }

        function prevPage() { if (currentPage > 1) { currentPage--; renderPage(); } }
        function nextPage() { if (currentPage < totalPages) { currentPage++; renderPage(); } }

        // ---------- Profile modal ----------
        async function showDetails(username, filename) {
            try {
                const response = await fetch(`/api/get_user_details?username=${encodeURIComponent(username)}&filename=${encodeURIComponent(filename)}`);
                const details = await response.json();
                if (details.error) { showToast(details.error, 'error'); return; }
                currentModalUser = { username, filename };
                const data = details.data;
                const friends = data.friends || [];
                const apiResponse = data.api_response || {};
                const accounts = apiResponse.accounts || [];
                const account = accounts.length ? accounts[0] : {};
                const rockstarAccount = account.rockstarAccount || {};
                const crews = account.crews || [];
                const linkedAccounts = account.linkedAccounts || [];
                const gamesOwned = rockstarAccount.gamesOwned || [];

                document.getElementById('modalTitle').textContent = details.username;
                let modalHtml = `
                    <div class="detail-section"><div class="detail-label">Rockstar ID</div><div class="detail-value">${escapeHtml(details.rockstar_id)}</div></div>
                    <div class="detail-section"><div class="detail-label">Source File</div><div class="detail-value">${escapeHtml(details.source_file)}</div></div>
                `;
                if (rockstarAccount.countryCode) modalHtml += `<div class="detail-section"><div class="detail-label">Country</div><div class="detail-value">${escapeHtml(rockstarAccount.countryCode)}</div></div>`;
                if (rockstarAccount.status) modalHtml += `<div class="detail-section"><div class="detail-label">Status</div><div class="detail-value">${escapeHtml(rockstarAccount.status)}</div></div>`;
                if (data.last_scanned) modalHtml += `<div class="detail-section"><div class="detail-label">Last Scanned</div><div class="detail-value">${new Date(data.last_scanned * 1000).toLocaleString()}</div></div>`;
                if (gamesOwned.length) modalHtml += `<div class="detail-section"><div class="detail-label">Games Owned (${gamesOwned.length})</div><div class="games-list">${gamesOwned.map(g => `<div class="game-tag">${escapeHtml(g.name)} (${escapeHtml(g.platform || 'unknown')})</div>`).join('')}</div></div>`;
                if (crews.length) modalHtml += `<div class="detail-section"><div class="detail-label">Crews (${crews.length})</div><div class="crews-list">${crews.map(c => `<div class="crew-tag"><div class="crew-name">${escapeHtml(c.crewName)} [${escapeHtml(c.crewTag)}]</div><div class="crew-meta">${escapeHtml(c.crewMotto || 'No motto')} | ${c.memberCount} members | ${c.isPrimary ? 'Primary' : 'Secondary'}</div></div>`).join('')}</div></div>`;
                if (linkedAccounts.length) modalHtml += `<div class="detail-section"><div class="detail-label">Linked Accounts (${linkedAccounts.length})</div><div class="linked-accounts">${linkedAccounts.map(a => `<div class="account-tag"><span class="account-service">${escapeHtml(a.onlineServiceDisplayName || a.onlineService)}:</span><span>${escapeHtml(a.userName)}</span></div>`).join('')}</div></div>`;
                modalHtml += `<div class="detail-section"><div class="detail-label">Friends (${friends.length})</div><div class="friends-list">${friends.length ? friends.map(f => `<div class="friend-tag"><span>${escapeHtml(f.name)}</span><span class="friend-rid">#${f.rockstarId}</span></div>`).join('') : '<span style="color: var(--text-secondary); font-size: 13px;">No friends listed</span>'}</div></div>`;
                if (friends.length === 0) modalHtml += `<div style="display: flex; justify-content: center; margin-top: 12px;"><button class="btn btn-primary deduce-btn" id="deduceBtn" onclick="checkDeducedFriends()">Check Deduced Friends</button></div>`;
                document.getElementById('modalContent').innerHTML = modalHtml;
                document.getElementById('detailModal').classList.add('active');
            } catch(e) { showToast('Error loading details', 'error'); }
        }

        function closeModal() { document.getElementById('detailModal').classList.remove('active'); currentModalUser = null; }

        async function checkDeducedFriends() {
            if (!currentModalUser) return;
            const { username, filename } = currentModalUser;
            const btn = document.getElementById('deduceBtn');
            if (!btn) return;
            const existing = document.getElementById('deducedSection');
            if (existing) { existing.remove(); btn.textContent = 'Check Deduced Friends'; btn.disabled = false; return; }
            btn.disabled = true; btn.textContent = 'Loading...';
            try {
                const response = await fetch(`/api/get_deduced_friends?username=${encodeURIComponent(username)}&filename=${encodeURIComponent(filename)}`);
                const result = await response.json();
                const entries = Object.entries(result);
                if (entries.length === 0) { showToast('No deduced friends found.', 'error'); btn.textContent = 'Check Deduced Friends'; btn.disabled = false; return; }
                let html = '<div class="deduced-friends-section" id="deducedSection"><div class="detail-label" style="margin-top: 8px;">Deduced Friends</div>';
                for (const [file, friends] of entries) {
                    if (friends.length === 0) continue;
                    html += `<div class="deduced-subheader">From ${escapeHtml(file)}</div><div class="deduced-friends-list">`;
                    for (const f of friends) html += `<div class="friend-tag"><span>${escapeHtml(f.username)}</span><span class="friend-rid">#${escapeHtml(f.rockstar_id)}</span></div>`;
                    html += '</div>';
                }
                html += '</div>';
                const buttonContainer = btn.parentNode;
                buttonContainer.insertAdjacentHTML('afterend', html);
                btn.textContent = 'Hide Deduced Friends'; btn.disabled = false;
            } catch(e) { showToast('Error fetching deduced friends', 'error'); btn.textContent = 'Check Deduced Friends'; btn.disabled = false; }
        }

        // ======================== Graph JS ========================
        async function renderGraph() {
            const username = document.getElementById('graphSearchInput').value.trim();
            if (!username) { showToast('Enter a username or RID.', 'error'); return; }
            if (!currentFiles.length) { showToast('No file loaded. Make sure users.json is in the repo.', 'error'); return; }
            if (sigmaInstance) { sigmaInstance.kill(); sigmaInstance = null; graphInstance = null; }
            if (pathfinderSigma) { pathfinderSigma.kill(); pathfinderSigma = null; pathfinderGraph = null; clearPathfinder(); }

            let targetUser = null, targetFile = null;
            try {
                const response = await fetch(`/api/search_by_username?username=${encodeURIComponent(username)}`);
                const results = await response.json();
                const firstFile = Object.keys(results)[0];
                if (firstFile && results[firstFile].length > 0) { targetUser = results[firstFile][0]; targetFile = firstFile; }
            } catch(e) { showToast('Search failed: ' + e.message, 'error'); return; }
            if (!targetUser) { showToast('User not found.', 'error'); return; }

            const depth = parseInt(document.getElementById('graphDepthSlider').value) || 2;
            showLoading();
            try {
                const response = await fetch(`/api/get_ego_graph?username=${encodeURIComponent(targetUser.username)}&filename=${encodeURIComponent(targetFile)}&depth=${depth}`);
                const graphData = await response.json();
                if (graphData.error) { showToast('Error: ' + graphData.error, 'error'); hideLoading(); return; }
                if (graphData.warning) showToast(graphData.warning, 'warning');
                renderSigma(graphData);
            } catch(e) { showToast('Graph rendering failed: ' + e.message, 'error'); }
            hideLoading();
        }

        function renderSigma(graphData) {
            const container = document.getElementById('graphContainer');
            if (sigmaInstance) { sigmaInstance.kill(); sigmaInstance = null; graphInstance = null; }
            container.innerHTML = '';
            if (!graphData.nodes || graphData.nodes.length === 0) {
                container.innerHTML = `<div id="graphLegend" style="position: absolute; bottom: 16px; left: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 13px; color: var(--text-secondary); display: flex; gap: 16px; z-index: 10;"><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#ff4444;"></span> Root</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#4caf50;"></span> Direct Friends</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#ffeb3b;"></span> Friends of Friends</span></div><div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: var(--text-secondary); pointer-events: none;">No connections found for this user.</div>`;
                return;
            }
            const Graph = window.graphology;
            if (!Graph) { showToast('Graph library not loaded.', 'error'); return; }
            graphInstance = new Graph();
            const adjacency = {};
            const degree1Nodes = [], degree2Nodes = [];
            let rootNode = null;
            for (const node of graphData.nodes) {
                if (node.degree === 0) rootNode = node;
                else if (node.degree === 1) degree1Nodes.push(node);
                else if (node.degree === 2) degree2Nodes.push(node);
            }
            for (const edge of graphData.edges) {
                if (!adjacency[edge.source]) adjacency[edge.source] = [];
                if (!adjacency[edge.target]) adjacency[edge.target] = [];
                adjacency[edge.source].push(edge.target);
                adjacency[edge.target].push(edge.source);
            }
            const visited = new Set();
            const communities = [];
            function findConnectedComponent(startId, maxDepth = 2) {
                const component = []; const queue = [[startId, 0]]; const localVisited = new Set();
                while (queue.length > 0) {
                    const [currentId, depth] = queue.shift();
                    if (localVisited.has(currentId)) continue;
                    localVisited.add(currentId);
                    const node = graphData.nodes.find(n => n.id === currentId);
                    if (node && node.degree === 1) component.push(currentId);
                    if (depth < maxDepth && adjacency[currentId]) {
                        for (const neighbor of adjacency[currentId]) {
                            if (!localVisited.has(neighbor)) queue.push([neighbor, depth + 1]);
                        }
                    }
                }
                return component;
            }
            for (const node of degree1Nodes) {
                if (!visited.has(node.id)) {
                    const component = findConnectedComponent(node.id);
                    if (component.length > 0) { communities.push(component); component.forEach(id => visited.add(id)); }
                }
            }
            const nodeToCommunity = {};
            communities.forEach((comm, idx) => { comm.forEach(nodeId => { nodeToCommunity[nodeId] = idx; }); });
            const nodeToParent = {};
            for (const edge of graphData.edges) {
                const sourceNode = graphData.nodes.find(n => n.id === edge.source);
                const targetNode = graphData.nodes.find(n => n.id === edge.target);
                if (sourceNode && targetNode) {
                    if (targetNode.degree === 1 && sourceNode.degree === 0) nodeToParent[targetNode.id] = sourceNode.id;
                    else if (targetNode.degree === 2 && sourceNode.degree === 1) nodeToParent[targetNode.id] = sourceNode.id;
                }
            }
            const R1 = 300, R2 = 550;
            const nodePositions = {};
            if (rootNode) nodePositions[rootNode.id] = { x: 0, y: 0 };
            const totalCommunities = Math.max(communities.length, 1);
            const anglePerCommunity = (2 * Math.PI) / totalCommunities;
            communities.forEach((community, commIdx) => {
                const baseAngle = commIdx * anglePerCommunity;
                const angleSpread = anglePerCommunity * 0.8;
                const angleStep = community.length > 1 ? angleSpread / (community.length - 1) : 0;
                community.forEach((nodeId, idx) => {
                    const angle = baseAngle + (idx * angleStep) - (angleSpread / 2) + (anglePerCommunity / 2);
                    nodePositions[nodeId] = { x: R1 * Math.cos(angle), y: R1 * Math.sin(angle) };
                });
            });
            const isolatedDegree1 = degree1Nodes.filter(n => !(n.id in nodeToCommunity));
            if (isolatedDegree1.length > 0) {
                const angleStep = (2 * Math.PI) / isolatedDegree1.length;
                isolatedDegree1.forEach((node, idx) => {
                    const angle = idx * angleStep;
                    nodePositions[node.id] = { x: R1 * Math.cos(angle), y: R1 * Math.sin(angle) };
                });
            }
            const parentToChildren = {};
            for (const node of degree2Nodes) {
                const parent = nodeToParent[node.id];
                if (parent) { if (!parentToChildren[parent]) parentToChildren[parent] = []; parentToChildren[parent].push(node); }
            }
            for (const [parentId, children] of Object.entries(parentToChildren)) {
                const parentPos = nodePositions[parentId];
                if (!parentPos) continue;
                const parentAngle = Math.atan2(parentPos.y, parentPos.x);
                const arcSize = Math.PI / 3;
                const angleStep = children.length > 1 ? arcSize / (children.length - 1) : 0;
                children.forEach((child, idx) => {
                    const angle = parentAngle - (arcSize / 2) + (idx * angleStep);
                    const radiusVariation = (Math.random() - 0.5) * 50;
                    const r = R2 + radiusVariation;
                    nodePositions[child.id] = { x: r * Math.cos(angle), y: r * Math.sin(angle) };
                });
            }
            for (const node of graphData.nodes) {
                const pos = nodePositions[node.id] || { x: (Math.random() - 0.5) * 100, y: (Math.random() - 0.5) * 100 };
                let color, size;
                if (node.degree === 0) { color = '#ff4444'; size = 15; }
                else if (node.degree === 1) { color = '#4caf50'; size = 10; }
                else { color = '#ffeb3b'; size = 7; }
                graphInstance.mergeNode(node.id, {
                    label: node.label, x: pos.x, y: pos.y, size: size, color: color,
                    username: node.username, source_file: node.source_file, rockstar_id: node.rockstar_id, degree: node.degree
                });
            }
            for (const edge of graphData.edges) {
                if (graphInstance.hasNode(edge.source) && graphInstance.hasNode(edge.target)) {
                    const targetNode = graphData.nodes.find(n => n.id === edge.target);
                    let edgeColor = '#4caf50';
                    if (targetNode && targetNode.degree === 2) edgeColor = '#ffeb3b';
                    graphInstance.mergeEdge(edge.source, edge.target, { color: edgeColor, size: (targetNode && targetNode.degree === 2) ? 1 : 2 });
                }
            }
            const renderer = new window.Sigma(graphInstance, container, {
                renderLabels: true, labelThreshold: 4, labelSize: 12, labelFont: 'Inter',
                labelColor: { color: '#e8e8ec' }, defaultNodeColor: '#64b5f6', defaultEdgeColor: '#4caf50',
                minCameraRatio: 0.05, maxCameraRatio: 10
            });
            const legend = document.createElement('div');
            legend.id = 'graphLegend';
            legend.style.cssText = 'position: absolute; bottom: 16px; left: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 13px; color: var(--text-secondary); display: flex; gap: 16px; z-index: 10; cursor: default;';
            legend.innerHTML = `<span class="legend-item" style="display:flex;align-items:center;gap:6px;cursor:pointer;" onclick="resetGraphRoot()"><span style="width:12px;height:12px;border-radius:50%;background:#ff4444;"></span> <strong>${rootNode ? rootNode.label : 'Root'}</strong> (click to reset)</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#4caf50;"></span> Direct (${degree1Nodes.length})</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#ffeb3b;"></span> 2nd Degree (${degree2Nodes.length})</span>`;
            container.appendChild(legend);
            window.currentGraphRoot = graphData.root_rid || (rootNode && rootNode.rockstar_id);
            window.currentGraphUsername = rootNode ? rootNode.username : null;
            window.currentGraphFile = rootNode ? rootNode.source_file : null;
            document.getElementById('resetGraphBtn').style.display = 'inline-block';
            renderer.on('clickNode', ({ node }) => {
                const attrs = graphInstance.getNodeAttributes(node);
                if (attrs.degree === 0) showDetails(attrs.username, attrs.source_file);
                else recenterGraph(attrs.rockstar_id, attrs.username);
            });
            renderer.on('doubleClickNode', ({ node }) => {
                const attrs = graphInstance.getNodeAttributes(node);
                if (attrs.username && attrs.source_file) showDetails(attrs.username, attrs.source_file);
            });
            renderer.getCamera().animatedReset({ duration: 600 });
            sigmaInstance = renderer;
        }

        async function recenterGraph(rid, username) {
            showLoading();
            try {
                const response = await fetch('/api/change_graph_root', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({rid}) });
                const graphData = await response.json();
                if (graphData.error) { showToast(graphData.error, 'error'); hideLoading(); return; }
                renderSigma(graphData);
                showToast(`Now viewing: ${username}`, 'success');
            } catch(e) { showToast('Failed to recenter graph', 'error'); }
            hideLoading();
        }

        function resetGraphRoot() {
            if (window.currentGraphUsername && window.currentGraphFile) {
                document.getElementById('graphSearchInput').value = window.currentGraphUsername;
                renderGraph();
            }
        }

        function clearGraph() {
            if (sigmaInstance) { sigmaInstance.kill(); sigmaInstance = null; graphInstance = null; }
            window.currentGraphRoot = null; window.currentGraphUsername = null; window.currentGraphFile = null;
            const container = document.getElementById('graphContainer');
            container.innerHTML = `<div id="graphLegend" style="position: absolute; bottom: 16px; left: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 13px; color: var(--text-secondary); display: flex; gap: 16px; z-index: 10;"><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#ff4444;"></span> Root</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#4caf50;"></span> Direct Friends</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#ffeb3b;"></span> Friends of Friends</span></div><div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: var(--text-secondary); pointer-events: none;">Enter a username and click Render.</div>`;
            document.getElementById('resetGraphBtn').style.display = 'none';
        }

        // ======================== Pathfinder JS ========================
        async function findConnectionPaths() {
            const startInput = document.getElementById('pathStartInput').value.trim();
            const endInput = document.getElementById('pathEndInput').value.trim();
            const maxDepth = parseInt(document.getElementById('pathDepthSlider').value);
            const maxPaths = parseInt(document.getElementById('pathMaxPathsSlider').value);
            if (!startInput || !endInput) { showToast('Enter both users.', 'error'); return; }
            if (!currentFiles.length) { showToast('No file loaded. Make sure users.json is in the repo.', 'error'); return; }
            if (sigmaInstance) { sigmaInstance.kill(); sigmaInstance = null; graphInstance = null; clearGraph(); }
            showLoading();
            document.getElementById('pathfinderStatus').textContent = 'Searching for connections...';
            try {
                let startRid = startInput, endRid = endInput;
                if (!/^\\d+$/.test(startInput)) {
                    const resp = await fetch(`/api/search_by_username?username=${encodeURIComponent(startInput)}`);
                    const results = await resp.json();
                    const firstFile = Object.keys(results)[0];
                    if (firstFile && results[firstFile].length > 0) startRid = results[firstFile][0].rockstar_id;
                    else throw new Error(`Could not find user: ${startInput}`);
                }
                if (!/^\\d+$/.test(endInput)) {
                    const resp = await fetch(`/api/search_by_username?username=${encodeURIComponent(endInput)}`);
                    const results = await resp.json();
                    const firstFile = Object.keys(results)[0];
                    if (firstFile && results[firstFile].length > 0) endRid = results[firstFile][0].rockstar_id;
                    else throw new Error(`Could not find user: ${endInput}`);
                }
                const response = await fetch('/api/find_connection_paths', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ start_rid: startRid, end_rid: endRid, max_depth: maxDepth, max_paths: maxPaths })
                });
                const result = await response.json();
                if (result.error) { document.getElementById('pathfinderStatus').innerHTML = `<span style="color: #ff8a9e;">${escapeHtml(result.error)}</span>`; hideLoading(); return; }
                renderPathfinderGraph(result);
                const resultsDiv = document.getElementById('pathResults');
                const contentDiv = document.getElementById('pathResultsContent');
                resultsDiv.style.display = 'block';
                let html = `<div style="margin-bottom: 12px; color: var(--text-secondary);">Found <strong>${result.path_count}</strong> connection path(s) between <strong>${escapeHtml(result.start.username)}</strong> and <strong>${escapeHtml(result.end.username)}</strong>${result.shortest_length ? ` (shortest: ${result.shortest_length} hops)` : ''}</div>`;
                html += '<div style="display: flex; flex-direction: column; gap: 8px;">';
                result.paths.forEach((path, idx) => {
                    html += `<div style="background: var(--bg); padding: 12px; border-radius: 6px; border-left: 3px solid ${idx === 0 ? '#4caf50' : '#64b5f6'};"><div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 4px;">Path ${idx + 1} (${path.length - 1} hops)</div><div style="display: flex; align-items: center; flex-wrap: wrap; gap: 4px;">`;
                    path.forEach((rid, i) => {
                        const node = result.nodes.find(n => n.id === rid);
                        const name = node ? node.label : rid;
                        html += `<span style="background: var(--surface); padding: 4px 10px; border-radius: 20px; font-size: 13px; font-weight: 500;">${escapeHtml(name)}</span>`;
                        if (i < path.length - 1) html += `<span style="color: var(--text-secondary);">→</span>`;
                    });
                    html += `</div></div>`;
                });
                html += '</div>';
                contentDiv.innerHTML = html;
                document.getElementById('pathfinderStatus').innerHTML = `<span style="color: #80d080;">Found ${result.path_count} path(s)</span>`;
            } catch(e) {
                document.getElementById('pathfinderStatus').innerHTML = `<span style="color: #ff8a9e;">Error: ${escapeHtml(e.message)}</span>`;
                showToast('Failed to find connection: ' + e.message, 'error');
            }
            hideLoading();
        }

        function renderPathfinderGraph(pathData) {
            const container = document.getElementById('pathfinderContainer');
            if (pathfinderSigma) { pathfinderSigma.kill(); pathfinderSigma = null; pathfinderGraph = null; }
            container.innerHTML = '';
            const Graph = window.graphology;
            if (!Graph) { showToast('Graph library not loaded.', 'error'); return; }
            pathfinderGraph = new Graph();
            pathData.nodes.forEach(node => {
                let color, size;
                if (node.type === 'start') { color = '#4caf50'; size = 15; }
                else if (node.type === 'end') { color = '#ff4444'; size = 15; }
                else { color = '#64b5f6'; size = 10; }
                pathfinderGraph.mergeNode(node.id, { label: node.label, color, size, username: node.username, source_file: node.source_file, rockstar_id: node.rockstar_id, in_paths: node.in_paths });
            });
            pathData.edges.forEach(edge => {
                if (pathfinderGraph.hasNode(edge.source) && pathfinderGraph.hasNode(edge.target)) {
                    const pathColors = ['#4caf50', '#2196f3', '#ff9800', '#9c27b0', '#00bcd4'];
                    const color = pathColors[edge.path_index % pathColors.length];
                    pathfinderGraph.mergeEdge(edge.source, edge.target, { color, size: 2 + (edge.path_index === 0 ? 1 : 0), path_index: edge.path_index });
                }
            });
            const startNode = pathData.nodes.find(n => n.type === 'start');
            const endNode = pathData.nodes.find(n => n.type === 'end');
            if (startNode && endNode && pathData.paths.length > 0) {
                const mainPath = pathData.paths[0];
                const nodeSpacing = 200;
                const totalWidth = (mainPath.length - 1) * nodeSpacing;
                mainPath.forEach((rid, idx) => {
                    if (pathfinderGraph.hasNode(rid)) {
                        const x = (idx * nodeSpacing) - (totalWidth / 2);
                        const y = 0;
                        pathfinderGraph.setNodeAttribute(rid, 'x', x);
                        pathfinderGraph.setNodeAttribute(rid, 'y', y);
                    }
                });
                let altPathOffset = 150;
                pathData.paths.slice(1).forEach((altPath, pathIdx) => {
                    altPath.forEach((rid, idx) => {
                        if (pathfinderGraph.hasNode(rid)) {
                            const existingX = pathfinderGraph.getNodeAttribute(rid, 'x');
                            if (existingX === undefined) {
                                const x = (idx * nodeSpacing) - (totalWidth / 2);
                                const y = (pathIdx + 1) * altPathOffset * (pathIdx % 2 === 0 ? 1 : -1);
                                pathfinderGraph.setNodeAttribute(rid, 'x', x);
                                pathfinderGraph.setNodeAttribute(rid, 'y', y);
                            }
                        }
                    });
                });
            }
            const renderer = new window.Sigma(pathfinderGraph, container, {
                renderLabels: true, labelThreshold: 0, labelSize: 12, labelFont: 'Inter',
                labelColor: { color: '#e8e8ec' }, minCameraRatio: 0.1, maxCameraRatio: 5,
                enableEdgeHovering: true, edgeHoverColor: '#ffffff', edgeHoverSizeRatio: 2
            });
            const legend = document.createElement('div');
            legend.id = 'pathfinderLegend';
            legend.style.cssText = 'position: absolute; bottom: 16px; left: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 13px; color: var(--text-secondary); display: flex; gap: 16px; z-index: 10;';
            legend.innerHTML = `<span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#4caf50;"></span> <strong>${escapeHtml(pathData.start.username)}</strong> (Start)</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#ff4444;"></span> <strong>${escapeHtml(pathData.end.username)}</strong> (End)</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#64b5f6;"></span> ${pathData.nodes.length - 2} Connections</span>`;
            container.appendChild(legend);
            renderer.on('clickNode', ({ node }) => {
                const attrs = pathfinderGraph.getNodeAttributes(node);
                if (attrs.username && attrs.source_file) showDetails(attrs.username, attrs.source_file);
            });
            renderer.getCamera().animatedReset({ duration: 600 });
            pathfinderSigma = renderer;
        }

        function clearPathfinder() {
            document.getElementById('pathStartInput').value = '';
            document.getElementById('pathEndInput').value = '';
            document.getElementById('pathDepthSlider').value = 5;
            document.getElementById('pathDepthDisplay').textContent = '5';
            document.getElementById('pathMaxPathsSlider').value = 3;
            document.getElementById('pathMaxPathsDisplay').textContent = '3';
            document.getElementById('pathfinderStatus').textContent = '';
            document.getElementById('pathResults').style.display = 'none';
            if (pathfinderSigma) { pathfinderSigma.kill(); pathfinderSigma = null; pathfinderGraph = null; }
            const container = document.getElementById('pathfinderContainer');
            container.innerHTML = `<div id="pathfinderLegend" style="position: absolute; bottom: 16px; left: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 13px; color: var(--text-secondary); display: flex; gap: 16px; z-index: 10;"><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#4caf50;"></span> Start</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#ff4444;"></span> End</span><span class="legend-item"><span style="width:12px;height:12px;border-radius:50%;background:#64b5f6;"></span> Connection</span></div><div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: var(--text-secondary); pointer-events: none; text-align: center;"><div style="font-size: 48px; margin-bottom: 16px; opacity: 0.3;">🔗</div><div>Enter two users and click "Find Connection"</div><div style="font-size: 12px; margin-top: 8px; opacity: 0.7;">Discover how any two people are linked through mutual friends</div></div>`;
        }

        // ======================== Utilities ========================
        function switchTab(tabId) {
            if (sigmaInstance) { sigmaInstance.kill(); sigmaInstance = null; graphInstance = null; }
            if (pathfinderSigma) { pathfinderSigma.kill(); pathfinderSigma = null; pathfinderGraph = null; }
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById('tab-' + tabId).classList.add('active');
            document.querySelector(`.tab-btn[data-tab="${tabId}"]`).classList.add('active');
        }

        function showLoading() { document.getElementById('loading').classList.add('active'); }
        function hideLoading() { document.getElementById('loading').classList.remove('active'); }
        function showToast(message, type = 'success') {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = 'toast ' + type;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 3000);
        }
        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Init
        window.onload = function() {
            updateFileList();
            loadFilterOptions();
        };
    </script>
</body>
</html>
    """

# --------------------------------------------
# FLASK APP
# --------------------------------------------
app = Flask(__name__)
api = Api()

@app.route('/')
def index():
    return create_html()

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