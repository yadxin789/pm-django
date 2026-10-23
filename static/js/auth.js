// 通用的权限检查脚本

// 全局用户信息变量
let currentUser = null;

// 检查用户是否已登录
function checkLogin() {
    return fetch('/api/auth/me')
        .then(response => {
            if (!response.ok) {
                window.location.href = '/auth/login';
                return false;
            }
            return response.json();
        })
        .then(user => {
            if (user) {
                currentUser = user;
                return true;
            }
            window.location.href = '/auth/login';
            return false;
        })
        .catch(error => {
            console.error('Error checking login status:', error);
            window.location.href = '/auth/login';
            return false;
        });
}

// 检查用户是否为管理员
function checkAdmin() {
    return checkLogin().then(loggedIn => {
        if (!loggedIn) return false;
        if (currentUser.role !== 'admin') {
            alert('您没有管理员权限，无法访问此页面');
            window.location.href = '/';
            return false;
        }
        return true;
    });
}

// 退出登录
function logout() {
    // 获取CSRF令牌，添加空值检查
    let csrfToken = null;
    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (csrfMeta) {
        csrfToken = csrfMeta.getAttribute('content');
    }
    
    // 构建请求头
    const headers = {
        'Content-Type': 'application/json'
    };
    
    // 如果有CSRF令牌，添加到请求头
    if (csrfToken) {
        headers['X-CSRF-TOKEN'] = csrfToken;
    }
    
    fetch('/auth/logout', {
        method: 'POST',
        headers: headers
    })
    .then(response => {
        window.location.href = '/auth/login';
    })
    .catch(error => {
        console.error('Error logging out:', error);
        window.location.href = '/auth/login';
    });
}

// 获取当前用户信息
function getCurrentUser() {
    return currentUser;
}

// 添加用户信息到页面
function addUserInfo() {
    if (!currentUser) return;
    
    const userInfoElement = document.getElementById('user-info');
    
    if (userInfoElement) {
        // 如果页面已存在user-info元素，直接更新内容
        userInfoElement.textContent = `用户：${currentUser.name || '未知'} (${currentUser.role === 'admin' ? '管理员' : '普通用户'})`;
    } else {
        // 如果页面没有user-info元素，创建并添加到header
        const header = document.getElementById('header');
        if (!header) return;
        
        const userInfo = document.createElement('div');
        userInfo.id = 'user-info';
        userInfo.className = 'user-info';
        userInfo.textContent = `用户: ${currentUser.name || '未知'} (${currentUser.role === 'admin' ? '管理员' : '普通用户'})`;
        header.appendChild(userInfo);
    }
}

// 添加退出登录按钮到页面
function addLogoutButton() {
    // 检查是否已存在退出按钮
    const existingLogoutBtn = document.getElementById('logout-btn');
    if (existingLogoutBtn) {
        // 如果按钮已存在，添加点击事件
        existingLogoutBtn.addEventListener('click', function(e) {
            e.preventDefault();
            logout();
        });
        return;
    }
    
    // 如果按钮不存在，创建新按钮
    const header = document.getElementById('header');
    if (!header) return;
    
    const logoutBtn = document.createElement('button');
    logoutBtn.id = 'logout-btn';
    logoutBtn.textContent = '退出登录';
    logoutBtn.className = 'back-to-main';
    logoutBtn.style.top = '10px';
    logoutBtn.style.right = '10px';
    logoutBtn.style.left = 'auto';
    logoutBtn.onclick = logout;
    header.appendChild(logoutBtn);
}

// 添加查询按钮事件
function addQueryButton() {
    if (!currentUser) return;
    
    // 检查是否已存在查询按钮
    const existingQueryBtn = document.getElementById('query-btn');
    if (existingQueryBtn) {
        // 如果按钮已存在，添加点击事件
        existingQueryBtn.addEventListener('click', function(e) {
            e.preventDefault();
            window.location.href = '/query/date';
        });
        return;
    }
    
    // 如果是管理员，添加查询按钮
    if (currentUser.role === 'admin') {
        const header = document.getElementById('header');
        if (header && !document.getElementById('admin-query-btn')) {
            const queryBtn = document.createElement('button');
            queryBtn.id = 'admin-query-btn';
            queryBtn.textContent = '查询数据';
            queryBtn.className = 'back-to-main';
            queryBtn.style.top = '10px';
            queryBtn.style.right = '80px';
            queryBtn.style.left = 'auto';
            queryBtn.onclick = function() {
                window.location.href = '/query/date';
            };
            header.appendChild(queryBtn);
        }
    }
}

// 初始化权限检查
function initAuth() {
    checkLogin().then(loggedIn => {
        if (loggedIn) {
            addUserInfo();
            addLogoutButton();
            addQueryButton();
        }
    });
}

// 页面加载时执行权限检查
document.addEventListener('DOMContentLoaded', function() {
    initAuth();
});